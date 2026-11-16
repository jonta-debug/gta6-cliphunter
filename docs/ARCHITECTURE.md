# Architecture

Status: the end-to-end chat, capture, extraction, `clean_gaming` render and Telegram manual-review pipeline is implemented; AI scoring and publishing remain absent.

## Boundaries

GTA6 ClipHunter separates inexpensive orchestration from video processing. The Netcup VPS runs the controller only. One or more workers run stream capture, audio analysis, transcription and rendering on machines sized for those tasks.

```text
Twitch EventSub WebSocket
          |
          v
Controller API / monitor ----> PostgreSQL
  |  chat features                 |
  |  candidate fusion              | durable jobs/events/scores
  |  budget + policy               |
  v                                v
Durable queue --------------> Media worker(s)
                                | rolling segments
                                | audio events / ASR
                                | AI analysis (gated)
                                | FFmpeg render
                                v
                          object/file storage
                                |
                                v
                    Telegram review delivery
                    (score >= configured threshold)
```

No component publishes to TikTok, YouTube, Instagram or another social platform.

## Components

### Controller

- FastAPI health, administration and read-only review/status endpoints.
- Async Twitch stream-state monitoring (planned) and authenticated EventSub chat monitoring for configured channels.
- Per-channel rolling baselines and cheap chat feature extraction.
- Candidate fusion, deduplication, cooldowns and Event Hype Score (0–10).
- Durable job dispatch with idempotency keys, leases, retries and dead-letter state.
- AI budget policy, usage ledger and provider/model configuration.
- Telegram delivery state and manual-review decisions.

The controller must not invoke FFmpeg or retain continuous video. It stores metadata and small chat/signal windows only.

Implemented in Milestone 1: strict multi-file JSONL replay, per-streamer sliding windows, adaptive chat baselines, explainable Event Hype Score, cooldown/overlap deduplication, stable candidate IDs, SQLite candidate persistence and read-only FastAPI candidate endpoints.

Implemented in Milestone 2: one official EventSub WebSocket with up to five `channel.chat.message` subscriptions, official payload normalization, EventSub message-ID deduplication, keepalive health checks, Twitch-directed reconnect, unexpected-disconnect resubscription with exponential backoff, bounded live buffering and graceful shutdown. Startup and hourly token validation verify the client, token owner, scope and lifetime; broadcaster logins resolve through Helix Get Users. The live buffer invokes the same deterministic `detect_candidates` function as replay.

Implemented shadow detector: `chat-reaction-shadow-v1` independently matches reaction categories, discounts repeated users, compares overlapping windows with recent per-channel baselines, groups event waves, and persists explainable results in `shadow_candidates`. Shadow records have no path to the production `candidates` table or media workers.

Twitch credentials are environment-only. The current version requires a pre-obtained user access token with `user:read:chat`; OAuth acquisition and refresh are not yet automated. Diagnostic output contains operational counters only, never configuration or authorization values.

The `run` supervisor starts the chat monitor, a restartable media worker per resolved channel, and at most one leased Telegram callback poller. A shared shutdown event drains components and gives FFmpeg time to finalize its current segment. Noncritical media/Telegram failures are logged without terminating chat ingestion.

### Media worker

- Implemented first slice: an opt-in, separate process resolves an accessible Twitch media URL through a replaceable resolver, copies it into approximately two-second FFmpeg MPEG-TS/HLS segments, records program-date-time timestamps/discontinuities, and retains 120 seconds.
- Polls persisted candidates by streamer ID, protects overlapping segments, waits for post-roll, remuxes the selected range to MP4 and validates it with FFprobe.
- Automatically renders validated clips with the isolated `clean_gaming` preset: complete gameplay fitted over a blurred 1080x1920 background, H.264 video and preserved AAC gameplay audio.
- Deletes expired unprotected segments; one worker currently handles one channel, allowing later multi-channel supervision without coupling capture to chat ingestion.
- HLS temporary/empty segments are excluded, selected segments are FFprobed before concatenation, and shutdown sends FFmpeg SIGINT before escalating to terminate/kill.
- Planned: extracts low-rate mono audio and calculates loudness/onset features cheaply.
- Persists a candidate’s pre/post-roll before its buffer expires.
- Performs targeted ASR and optional multimodal/LLM analysis only after the hype gate.
- Calculates Viral Potential Score (0–10), renders approved candidates and uploads artifacts to shared storage.

Workers advertise capabilities (`capture`, `asr`, `render`, optional `gpu`) and enforce concurrency/resource limits. A job is safe to retry; artifact names derive from stable candidate IDs.

### Rendering modes

The renderer uses a preset interface so modes do not change the worker pipeline:

- `clean_gaming`: implemented; gameplay-first fit with blurred background and no crop.
- `tiktok_viral`: animated captions and event-driven zooms.
- `split_screen`: secondary gameplay only when it is licensed and provenance is recorded.
- `cinematic`: restrained pacing, transitions and color/audio treatment.

Modes must share the same source-range and artifact contracts; the controller must not perform rendering.

### AI gateway

One interface supports Gemini, OpenRouter, OpenAI-compatible endpoints and optional local models. Requests carry purpose, provider, model, estimated cost, candidate ID and idempotency key. Required policy:

- environment-based secrets; never log keys or raw authorization headers;
- structured outputs with schema validation;
- bounded exponential backoff with rate-limit handling;
- per-request and monthly spending limits checked before dispatch;
- token/cost recording for success and failed billable attempts;
- provider fallback only when policy permits it.

### Telegram delivery

Implemented: after successful rendering, create an idempotent SQLite review record and optionally send the video, channel, Event Hype score, timestamp and duration through the Telegram Bot API. A separate long-poll worker accepts tokenized callbacks only from configured user IDs in the configured chat. Approve finalizes immediately; Reject requires a persisted structured reason (`advertisement`, `boring`, `wrong_timing`, or `duplicate`). Delivery state and update offsets prevent normal restart duplicates; approval never triggers social publishing.

## Domain records

- `Streamer`: Twitch identity, enabled state, locale, rights/permission note and detector settings.
- `StreamSession`: online interval, worker lease, buffer status and source metadata.
- `SignalWindow`: aggregate chat/audio features; raw chat retention should be bounded.
- `Candidate`: time range, evidence, detector version, lifecycle and deduplication key.
- `Score`: `event_hype` and optional `viral_potential`, both 0–10, with component breakdown and version.
- `Artifact`: source fragment, rendered clip, subtitle/thumbnail metadata and retention state.
- `AIUsage`: provider/model/purpose, tokens, estimated cost, status and timestamps.
- `Review`: Telegram message identity, reviewer action and optional structured rejection reason.

## Pipeline and gates

1. Monitor stream state and chat asynchronously.
2. Compute cheap per-channel signals; normalize against recent robust baselines.
3. Fuse chat spike, unique-user breadth, repetition/bot penalty, audio energy/onset and cooldown into Event Hype Score.
4. If the configurable hype gate passes, persist the buffered source range.
5. Run targeted ASR and optional AI analysis under budget policy.
6. Calculate Viral Potential Score from hook, context independence, payoff, visual/action evidence and confidence.
7. Render on a media worker.
8. Deliver scores of at least the configurable review threshold to Telegram.
9. Await and record manual review.

Low-confidence or budget-blocked candidates remain inspectable; they do not silently consume paid AI.

## Storage and queue

Use PostgreSQL as source of truth in production and SQLite only for local development. Milestone 1 defines a candidate-store protocol with a SQLite implementation; PostgreSQL remains planned. Begin production job dispatch with a PostgreSQL-backed job table and `FOR UPDATE SKIP LOCKED` leases to avoid an extra broker; introduce Redis/RabbitMQ only if measured throughput requires it. Store video artifacts outside database rows, behind a storage interface that supports local disk initially and S3-compatible storage later.

## Reuse constraints

- PeakCut MIT code may be selectively adapted with copyright/license preservation.
- TwitchSnipBot and ClipForge are design references until compatible licenses are supplied.
- Never vendor the external repositories or commit their nested Git metadata.
- Avoid PeakCut’s social-posting modules and ClipForge’s face-tracking renderer in the initial system.

## Security and operations

- Validate Twitch URLs/identities and never pass untrusted strings through a shell.
- Isolate workers from controller credentials; workers receive scoped jobs and only the secrets they need.
- Bound upload/download size, buffer retention, command runtime, AI input size and worker concurrency.
- Redact chat/user data in logs and define retention before production use.
- Provide health/readiness checks and metrics for monitored channels, candidate rates, queue age, worker leases, AI spend and Telegram failures.
- Process and distribute only streams for which appropriate rights or permissions exist.
