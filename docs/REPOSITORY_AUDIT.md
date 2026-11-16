# Repository Audit

Audit date: 2026-11-16. The three repositories were inspected locally at their current `HEAD`; no dependency was installed and no reference file was changed. “Implemented” below means code exists, not that external services or full media pipelines were exercised.

## Executive decision

| Repository | Best contribution | Reuse decision |
|---|---|---|
| PeakCut | Live rolling buffer, loudness/chat triggers, provider abstraction, media and caption pipeline | Selectively adapt MIT-licensed code, preserving notices and attribution. Do not import the application wholesale. |
| TwitchSnipBot | Chat-window features and statistical spike detection | Use as a behavioral reference only. No repository license is present; do not copy code or the bundled ONNX model unless the author supplies a compatible license and model provenance. |
| ClipForge | Word-timed captions, transcript selection, reframing/rendering concepts | Use as a behavioral reference only. No repository license or bundled asset notices are present; do not copy code/assets without permission and provenance. |

PeakCut is the only current source that is legally ready for code reuse. Even there, its in-process/threaded application architecture is not suitable as GTA6 ClipHunter’s system boundary.

## PeakCut

**Revision:** `ed61d91f92f03fa23f8fc2f050d1162b667394d8`, 2026-11-15 13:50:16 +05:00 — “Storage: say plainly when the drive is full, and let the folder be opened and moved”. The inspected worktree was clean.

**Implemented features.** A FastAPI self-hosted application accepts uploads and YouTube/Twitch/Kick links, downloads/probes media, extracts audio, transcribes locally with sherpa-onnx, asks configurable AI providers to select clips, renders vertical clips with ASS captions, and stores jobs/users in SQLite or PostgreSQL. Live mode resolves stream URLs, records six-second MPEG-TS segments with FFmpeg, maintains a rolling buffer, ingests anonymous Twitch IRC, measures per-second loudness/chat, transcribes incrementally, and renders selected ranges. Live selection combines transcript and reaction-signal AI judges; fallback VOD selection uses loudness. Creator monitoring, fair per-user queues, load/disk guards, remote ASR, usage-token recording, authentication, and social posting are also implemented.

**Architecture and language.** Python 3.10+ monolith (`studio/`) with plain HTML/JavaScript UI, FastAPI/uvicorn, SQLAlchemy, background threads, in-memory queues/state, FFmpeg/yt-dlp subprocesses, and SQLite/PostgreSQL. Video work and web/controller work run in the same process/container by default; optional remote ASR is the only explicit heavy-worker split.

**Dependencies and license.** Pinned Python dependencies include FastAPI, httpx, NumPy, SQLAlchemy/psycopg, sherpa-onnx, soundfile, Pillow, uvicorn and yt-dlp; FFmpeg and Deno are system/runtime dependencies. The repository is MIT licensed. Bundled Anton, Archivo Black and Poppins fonts include SIL OFL texts.

**Reusable seams.** Prefer small, attributed ports of:

- `studio/live.py`: segment-buffer bookkeeping and audio/chat signal collection; move capture to a video worker and replace thread-per-task orchestration.
- `studio/media.py`: safe command invocation, probing, audio extraction and loudness calculation.
- `studio/ai.py`, `studio/usage.py`: provider normalization, structured JSON responses, key redaction, and usage-ledger concepts; add async I/O, retry/backoff, price accounting and hard budgets.
- `studio/brain.py`: untrusted-transcript delimiting, timestamp snapping and loudness fallback concepts; replace its 0–100 single score with the required two-stage 0–10 scores.
- `studio/render.py`: FFmpeg layouts and ASS-caption construction for a first renderer.
- `studio/jobs.py`: fair-queue concepts, not the in-memory implementation.

**Apple Silicon.** The code and subprocess approach are portable, the Docker base/stages are multi-architecture, and the README explicitly supports macOS. FFmpeg, yt-dlp, NumPy, Pillow and PostgreSQL have common arm64 paths. The main uncertainty is the exact sherpa-onnx/model wheel combination on Python 3.11 arm64; verify it in a dedicated worker spike. Local Docker runs also consume substantial memory during ASR/rendering.

**Risks and gaps.** It has only 14 commits and a large `server.py`, with substantial cross-module global state. Live capture, ASR, AI, rendering and API concerns share one process and local filesystem. AI calls are synchronous; model fallback exists but there is no spending limit, monetary accounting, or general retry/backoff policy. Token usage is tracked only after successful calls. Detection does not expose separate Event Hype and Viral Potential scores, Telegram is absent, and the social auto-post subsystem directly conflicts with this project’s manual-review-only rule. Periodic live AI judging could also violate the desired cheap-first cost envelope if reused unchanged.

## TwitchSnipBot

**Revision:** `7d3fa2fad8dee60c03617a30706214009012b1ea`, 2025-08-02 20:10:00 +05:30 — “webapp: proper svg favicon”. The inspected worktree was clean.

**Implemented features.** The Python core parses Twitch/BLAST/HLTV logs, ingests anonymous Twitch IRC, creates overlapping chat windows, extracts 12 chat features, labels statistical anomalies, trains/runs a two-head PyTorch LSTM, exports ONNX, detects live/batch moments, downloads VODs, cuts/compiles clips, stores SQLite records, and exposes FastAPI REST/WebSocket routes. A React/TypeScript browser application captures HLS, detects chat spikes, persists sessions and remuxes clips using ffmpeg.wasm. CS/HLTV event classifiers and plugin interfaces exist.

The scheduled `record_job`, `process_job`, and `upload_job` functions are placeholders rather than working orchestration. Match API data is a stub, CS mode is marked under development, and the Python dashboard is not a complete end-to-end recorder. The live statistical fallback also reads caps and emote feature indices in reverse; `--statistical` is parsed but not applied explicitly. A bundled ONNX model exists, but no training-data/model license or evaluation report is provided.

**Architecture and languages.** Python 3.10+ modular package for ingestion, features, labeling, model, pipeline, clipping, plugins, SQLite, scheduler and FastAPI; React/TypeScript/Vite applications for local dashboard and Cloudflare Pages; ffmpeg.wasm and ONNX Runtime Web in the browser. The Python CLI is explicitly alpha.

**Dependencies and license.** Python dependencies include PyTorch, NumPy, SciPy, PyYAML, aiohttp, Streamlink, ffmpeg-python, ONNX, FastAPI, APScheduler, websockets/httpx and optional Google upload libraries. Web dependencies include React, Vite, tmi.js, ONNX Runtime Web and ffmpeg.wasm. No LICENSE/COPYING/NOTICE file or package license declaration exists at `HEAD`; therefore the source and `webapp/public/models/highlight.onnx` are all-rights-reserved by default.

**Reusable seams.** If a compatible license is obtained, the strongest candidates are the chat message model, sliding windows, feature extraction, rolling statistical baseline/cooldown, and test fixtures. Until then, implement an independently written detector from the public behavior: message-rate baseline plus unique-user, repetition, emote, caps and keyword signals. Do not use the LSTM first; it adds PyTorch/ONNX weight and lacks a validated GTA 6 dataset.

**Apple Silicon.** Pure Python/NumPy chat detection is compatible. PyTorch supports arm64 macOS but the code chooses only CUDA or CPU, not MPS; training will therefore use CPU without modification. Streamlink/FFmpeg are portable. Browser WASM is portable but memory-heavy and irrelevant to a self-hosted worker. Exact Node/Cloudflare builds were not exercised.

**Risks and gaps.** Missing license and model provenance block copying. Several end-to-end paths are placeholders, tests cover features/parsers/labeling/model shapes but not live reconnects, scheduler, clipping, web APIs or browser capture, and there is no Telegram delivery, AI abstraction, cost control, audio-event detector, multi-stream resource policy, or production deployment. Statistical normalization is channel-agnostic and vulnerable to raids, bots, slow chat and category drift.

## ClipForge

**Revision:** `56c92c14d02a79fa166982e9c631c5929c12c660`, 2026-10-18 14:20:35 +01:00 — “Serve under a subpath (tukems.cloud/clipforge)”. The inspected worktree was clean.

**Implemented features.** A single-user FastAPI application downloads or uploads media, normalizes/probes with FFmpeg, transcribes using faster-whisper or an OpenAI-compatible speech endpoint, selects transcript moments through Claude tool use or a heuristic, and assigns a 0–99 virality score with four sub-scores. It provides word-level editing/filler removal, ASS/SRT captions, multiple aspect ratios, watermark text, YuNet face detection, shot detection, single-face tracking, two-face split screen, blurred-fit fallback, frame-by-frame OpenCV composition, SQLite persistence and an in-process priority queue. It produces MP4/SRT/TXT/ZIP outputs. There is no social posting.

**Architecture and language.** Python 3.12 FastAPI monolith with vanilla JavaScript UI, SQLite, thread workers, local project directories, FFmpeg subprocesses, OpenCV/NumPy per-frame rendering, Anthropic SDK, faster-whisper and yt-dlp. Queue state is in process; interrupted database jobs are reconstructed at startup.

**Dependencies and license.** Requirements are FastAPI, uvicorn, Anthropic SDK, faster-whisper, yt-dlp, OpenCV headless, NumPy and httpx; FFmpeg and Deno are system/runtime dependencies. The Dockerfile explicitly maps `arm64` to an aarch64 Deno artifact. The repository contains no project license. README claims bundled fonts are SIL OFL and the YuNet model is MIT/Apache, but no corresponding license/notice files are committed; verify each asset’s origin and terms before redistribution.

**Reusable seams.** No code or assets should be copied under the current license state. If relicensed, consider only `pipeline/captions.py`, transcript timestamp mapping/edit ranges, heuristic/AI schema ideas, and possibly blurred-fit rendering. Face tracking is a poor default for GTA gameplay: it is compute-heavy and will usually target a small facecam or fall back to fit.

**Apple Silicon.** Native CPU operation is plausible: OpenCV and NumPy commonly provide arm64 wheels, and the Dockerfile handles arm64 Deno. Risks are faster-whisper/CTranslate2 wheel and compute-mode support on the selected Python version, large model downloads, and very high CPU/RAM bandwidth from raw 1080p frame pipes. Docker on a 16 GB Mac should use one render worker. None of these paths was executed in this audit.

**Risks and gaps.** Only two commits, no tests, no repository license, unverified bundled-asset provenance, single-user auth, in-process queue, local-only storage, no live monitoring/chat/audio event path, Anthropic-only selection, no AI usage/cost ledger or spending limit, and no controller/worker separation. Rendering every frame through Python/OpenCV is unsuitable for the lightweight VPS.

## Comparative recommendation

1. Build the controller and domain model independently in this repository.
2. Adapt only well-isolated MIT PeakCut internals after adding attribution and tests.
3. Independently implement a lightweight chat detector informed by TwitchSnipBot’s behavior; request a license before any literal reuse.
4. Start with FFmpeg-only crop/blur/captions. Defer face tracking and other ClipForge ideas until GTA 6 sample footage proves value and licensing is resolved.
5. Explicitly omit all social-upload/auto-post code. Telegram is a review transport, never a publishing trigger.

## Verification performed

- Inspected Git history/status, tracked files, manifests, Docker/deploy files, source, tests and bundled assets.
- Parsed all 125 Python files with `ast`: no syntax errors.
- Ran TwitchSnipBot tests without writing to its tree: 18 passed in 2.40 seconds.
- Did not run PeakCut integration tests because required sherpa-onnx is not installed and its runner launches the service.
- ClipForge has no test suite. External-service, FFmpeg, Docker, browser and Apple Silicon runtime paths were not executed.
