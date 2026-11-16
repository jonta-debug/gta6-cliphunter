# GTA6 ClipHunter — Agent Instructions

## Project Goal

Build a modular, self-hosted Twitch highlight detection and clipping system optimized for GTA 6 gameplay.

The system monitors 5–50 Twitch streamers, detects interesting moments, generates clips, and sends high-scoring candidates to Telegram for manual review.

Never automatically publish clips to social media.

## Development Environment

- macOS, Apple Silicon M3, 16 GB RAM
- Codex CLI
- Python 3.11+
- FastAPI
- FFmpeg
- Git
- Docker Compose

Production target: Netcup VPS Lite 1 G12s.

The VPS handles lightweight monitoring and orchestration. Video processing must support separate workers.

## Reference Repositories

Existing repositories are expected under:

- `external/peakcut/`
- `external/twitchsnipbot/`
- `external/clipforge/`

Treat these directories as read-only.

Do not clone them again unless explicitly requested.

Before reusing code:
1. Verify the actual implementation.
2. Check licenses and dependencies.
3. Identify reusable components.
4. Document integration decisions.

Do not blindly merge entire repositories.

## Architecture Principles

- Modular Python architecture.
- Prefer existing, tested components over rewriting.
- Keep dependencies minimal.
- Separate monitoring, detection, scoring, rendering, and delivery.
- Use asynchronous processing where appropriate.
- Support multiple concurrent streamers.
- Make AI providers interchangeable.
- Design for low resource consumption.
- Keep expensive video processing outside the lightweight VPS controller.

## Highlight Pipeline

1. Monitor Twitch streams and chat.
2. Detect chat spikes and audio events.
3. Generate highlight candidates.
4. Calculate an Event Hype Score (0–10).
5. Analyze promising candidates with AI.
6. Calculate a Viral Potential Score (0–10).
7. Extract and render clips.
8. Send clips scoring at least 7 to Telegram.
9. Wait for manual review.

The score threshold must be configurable.

## AI and Cost Optimization

Support:

- OpenRouter
- Gemini
- OpenAI-compatible APIs
- Optional local models

Use inexpensive detection methods before paid AI calls.

Avoid continuous paid analysis of full streams.

Implement:
- API usage tracking
- Configurable spending limits
- Request retries
- Rate-limit handling
- Model configuration via environment variables

## Security

- Never hardcode API keys.
- Never commit `.env` files or secrets.
- Provide `.env.example`.
- Never expose credentials in logs.
- Do not execute untrusted repository scripts without inspection.
- Do not deploy or publish without explicit approval.
- Only process and distribute content where appropriate rights or permissions exist.

## Development Workflow

For every implementation task:

1. Inspect relevant existing code.
2. Propose a concise implementation approach.
3. Modify only necessary files.
4. Add or update relevant tests.
5. Run available targeted tests.
6. Summarize changes and remaining issues.

Avoid unnecessary refactoring.

Do not install dependencies unless required.

Do not modify files inside `external/`.

## Git Rules

- Work in the main project directory.
- Preserve existing Git history.
- Never force-push.
- Never commit secrets.
- Keep commits small and focused.
- Do not commit or push without explicit approval.
- Do not include nested reference repositories in project commits.

## Documentation

Maintain:

- `docs/ARCHITECTURE.md`
- `docs/REPOSITORY_AUDIT.md`
- `docs/IMPLEMENTATION_PLAN.md`

Keep documentation concise and synchronized with actual implementation.

## Agent Behavior

- Prioritize correctness over speed.
- Avoid unnecessary token usage.
- Do not repeatedly scan the entire repository.
- Read only files relevant to the current task.
- Reuse existing documentation and findings.
- Ask before making major architectural changes.
- Do not claim features work without testing them.
- Clearly distinguish implemented functionality from planned functionality.
