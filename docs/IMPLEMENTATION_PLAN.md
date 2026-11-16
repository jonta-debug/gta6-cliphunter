# Implementation Plan

Status: Phase 1 and Milestone 1 are complete. Milestone 2 real-time Twitch chat was implemented on 2026-11-16; later milestones remain planned.

## Milestone 1 — Detection foundation

Goal: prove cheap, deterministic candidate generation without video rendering or paid AI.

- **Implemented:** Python package, typed detector settings, `.env.example`, health endpoint and a database-neutral candidate-store protocol with SQLite implementation.
- **Implemented:** `ChatMessage`, `SignalWindow` and `Candidate` records with versioned detector evidence. Live `Streamer` and `StreamSession` lifecycle records are deferred until live monitoring.
- **Implemented:** strict multi-file JSONL replay with numeric/ISO timestamps, multiple streamers, stable ordering and line-specific malformed-input errors.
- **Implemented independently:** per-streamer rolling baselines, overlapping windows, message-frequency surprise, relative activity, participant, emote and caps signals.
- **Implemented:** deterministic weighted Event Hype Score 0–10, configurable threshold/weights/scales, stable candidate IDs, suggested clip ranges and cooldown/overlap deduplication.
- **Implemented:** synthetic pytest scenarios for normal chat, small/large streamer spikes, duplicates, multiple streamers, empty/malformed input, determinism, persistence, CLI and API.
- **Implemented:** read-only `GET /health`, `GET /candidates` and `GET /candidates/{id}` endpoints.
- **Deferred by current scope:** live anonymous Twitch IRC, reconnect supervision and live load testing.

Current verification: replay creates stable persisted candidates; the realistic 574-message acceptance replay produces only its two planted events; and 16 targeted tests pass on macOS arm64 using Python 3.13. Python 3.11 and Linux CI, long-duration bounded-memory profiling, and live supervision remain unverified.

Run instructions and the accepted JSONL schema are in `README.md`.

## Milestone 2 — Real-time Twitch chat integration

- **Implemented:** official EventSub WebSocket client for `channel.chat.message` version 1.
- **Implemented:** environment configuration for a user access token, client ID, authenticated user ID and up to five broadcaster IDs or logins.
- **Implemented:** startup and hourly `/validate` checks for user-token identity, Client ID, `user:read:chat` and remaining lifetime; broadcaster login resolution through Helix Get Users.
- **Implemented:** normalization into the shared `ChatMessage`, retaining broadcaster/user IDs, text, emote objects, badges, replies, cheers, message type and shared-chat attributes.
- **Implemented:** one connection with concurrent channel subscriptions, at-least-once message-ID deduplication and invalid-payload isolation.
- **Implemented:** keepalive timeout monitoring, unexpected-disconnect resubscription, bounded exponential backoff, server-directed reconnect URLs and graceful shutdown.
- **Implemented:** bounded live buffering that calls the existing offline detector and existing candidate store without duplicating scoring logic.
- **Implemented:** mocked transport tests; no live credentials or network are required by tests.
- **Implemented:** redacted `twitch-diagnose` operational output for socket status, active subscriptions, received messages and detected candidates.
- **Not implemented:** OAuth acquisition/refresh, AI or Telegram. Video capture and initial rendering are implemented in later slices.

Exit criteria: mocked messages from multiple channels reach the shared detector; duplicates and invalid payloads are suppressed; disconnect, keepalive timeout and reconnect paths recover; shutdown closes the active socket.

Current verification: 46 tests pass, including authentication validation, broadcaster-login resolution, mocked EventSub messages, reconnect behavior, diagnostic redaction, deterministic detection, persistence and API coverage. A credentialed Twitch network test remains an operator step.

## Milestone 3 — Capture and audio worker

- **Implemented:** isolated one-channel media-worker process with a replaceable source resolver and conservative yt-dlp Twitch resolver.
- **Implemented:** FFmpeg stream-copy segments with program-date-time metadata, discontinuity tracking, 120-second cleanup and protected candidate ranges.
- **Implemented:** SQLite candidate polling, pre/post-roll waiting, MP4 remux, FFprobe validation, idempotent artifact names and clean process shutdown.
- **Implemented:** generated-media integration coverage; automated tests require no Twitch stream.
- Define durable production job leases and worker capability/heartbeat contracts before remote workers.
- Extract mono low-rate audio and add loudness/onset signals to Event Hype scoring.
- Test crash recovery, resolver refresh, duplicate jobs and storage limits under long-running capture.

Exit criteria: a candidate reliably preserves a playable source fragment while the controller performs no video work.

## Milestone 4 — Targeted AI and cost controls

- Add ASR behind local and remote interfaces; benchmark sherpa-onnx and faster-whisper compatibility on M3 before choosing a default.
- Add Gemini, OpenRouter, OpenAI-compatible and local provider adapters.
- Add schema validation, retries/backoff, usage/cost ledger, per-request/monthly caps and budget-denied states.
- Generate Viral Potential Score 0–10 only for hype-gated candidates; version prompts and score formulas.
- Add provider contract tests using fakes; no live paid calls in the default test suite.

Exit criteria: a fixture candidate is analyzed through each fake provider, costs are recorded, and hard limits prevent dispatch.

## Milestone 5 — Rendering and Telegram review

- **Implemented ahead of this milestone:** modular FFmpeg/FFprobe renderer and automatic `clean_gaming` 1080x1920 output with full-frame gameplay, blurred fill, H.264 and AAC.
- **Implemented:** optional Telegram Bot API video delivery with persistent pending/approved/rejected reviews, authorized tokenized callbacks, retry/error isolation and restart deduplication.
- Add `tiktok_viral`, `split_screen` and `cinematic` only in later approved work. Split-screen secondary footage must be licensed and provenance recorded.
- Add captions, audio normalization and thumbnail generation later; selectively adapt MIT PeakCut helpers with attribution.
- Add renderer capability/concurrency limits and idempotent artifacts.
- Add Viral Potential threshold gating after AI scoring exists. Approval remains a review state only and never publishes externally.

Exit criteria: qualifying fixtures render once, arrive in Telegram once and retain a durable manual decision.

## Milestone 6 — Production hardening

- **Implemented:** single-command async supervision for Twitch chat, per-channel media workers and optional Telegram callbacks with coordinated signal shutdown.
- **Implemented:** media resolver/worker restart backoff, channel-specific storage, corrupt artifact replacement, graceful final-segment handling, temporary/empty segment exclusion and pre-concat FFprobe checks.
- **Implemented:** SQLite callback-poller lease, restart-safe artifact validation and existing review delivery deduplication.
- Add Twitch authenticated stream-state monitoring where required, 5–50 channel load tests, metrics and alerts.
- Add S3-compatible artifact storage and remote worker deployment profiles.
- Define retention/privacy policy, backup/restore, rights metadata and operator runbooks.
- Calibrate GTA 6 scoring against manually labeled streams and measure precision, recall, cost per reviewed clip and time-to-review.
- Consider an ML detector or advanced reframing only after labeled-data and profiling evidence justify them.

## Deferred or excluded

- Any automatic social-media publishing: permanently excluded.
- TwitchSnipBot/ClipForge code or bundled models/assets: blocked until compatible licensing and provenance are documented.
- LSTM/ONNX chat inference: deferred until a GTA 6 dataset beats the statistical baseline.
- Face tracking and Python/OpenCV per-frame rendering: deferred; costly and low-value for gameplay-first footage.
- Browser-side HLS capture/ffmpeg.wasm and a full dashboard: not needed for the first operational pipeline.

## First implementation slice

Start Milestone 1 with domain records plus offline chat-log replay, not a live Twitch connection. Replay makes the score formula and persistence testable before network behavior is introduced. The first vertical slice should parse a small synthetic chat fixture, emit a deduplicated candidate with Event Hype Score 0–10, persist its component evidence, and expose it from FastAPI.
