# GTA6 ClipHunter

GTA6 ClipHunter provides deterministic replay plus a live chat-to-video manual-review pipeline. It does not call paid AI or publish content.

## Complete pipeline

After loading the git-ignored `.env`, one command starts Twitch chat detection, one supervised media worker per configured Twitch channel, automatic extraction/rendering/review delivery, and the optional Telegram callback poller:

```bash
set -a; source .env; set +a
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m cliphunter run \
  --database data/cliphunter.db \
  --buffer-root data/buffer \
  --clips-root data/clips \
  --rendered-root data/rendered
```

`Ctrl+C` requests coordinated shutdown and graceful FFmpeg finalization. Media is organized under per-channel directories. Offline streams and failed media workers are retried with bounded exponential backoff; Telegram remains disabled when all `TELEGRAM_*` variables are empty. No approval action publishes content.

## JSONL input

Each non-empty line is a JSON object:

```json
{"streamer_id":"channel-name","timestamp":81.2,"username":"viewer","text":"NO WAY","emotes":["Pog"]}
```

`timestamp` accepts Unix seconds or an ISO-8601 string. `emotes` may be a list or non-negative count; `emote_count` and `is_emote` are also accepted. Malformed records stop replay with the file and line number.

## Run a replay

From the project root:

```bash
PYTHONPATH=src python3 -m cliphunter replay chat.jsonl --database data/cliphunter.db
```

Multiple JSONL files may be supplied. Detector and database settings are documented in `.env.example` and read from environment variables.

Start the read-only API:

```bash
PYTHONPATH=src python3 -m cliphunter serve --host 127.0.0.1 --port 8000
```

Endpoints:

- `GET /health`
- `GET /candidates?streamer_id=...&min_score=...&limit=...`
- `GET /candidates/{id}`

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest
```

The synthetic suite covers normal activity, small and large channels, spikes, overlapping duplicate events, multiple streamers, empty files, malformed records, deterministic replay, SQLite persistence, CLI replay and API responses.

## Real-time Twitch chat

The live listener uses Twitch's official EventSub WebSocket transport and `channel.chat.message` version 1. It opens one socket and creates one subscription for each configured broadcaster ID.

### Authentication

1. Sign in to Twitch with a verified email address and enable two-factor authentication.
2. Open **Twitch Developer Console → Applications → Register Your Application**. Enter a unique name, an OAuth Redirect URL that exactly matches the callback used by your token-acquisition flow, select the category that describes the application, complete the CAPTCHA, and create it. For the Twitch CLI flow below, register `http://localhost:3000` as a redirect URL. Open **Manage** and copy the Client ID. If using Authorization Code flow, generate a Client Secret and keep it outside this repository; ClipHunter does not need the secret at runtime.
3. Configure the official Twitch CLI with that application, then obtain a **user access token** for the operator account with only `user:read:chat`: `twitch token --user-token --scopes "user:read:chat"`. The command opens Twitch consent in a browser and prints sensitive access/refresh tokens; do not paste its output into tickets, logs or version control. For unattended operation, use Authorization Code flow and store its refresh token in a secret manager; automatic acquisition and refresh are not implemented yet.
4. Record the token owner's numeric user ID as `TWITCH_USER_ID`. EventSub requires the WebSocket subscription's `user_id` to match the user represented by the user access token.
5. Configure up to five broadcaster logins in `TWITCH_CHANNELS`; ClipHunter resolves them with the official Helix Get Users API. Numeric `TWITCH_CHANNEL_IDS` remain available as an alternative.
6. Export values in the process environment. For local use, copy `.env.example` to a git-ignored file outside the repository or to `.env`, restrict it with `chmod 600`, fill it in, and source it before starting ClipHunter. The application does not auto-load `.env`:

```ini
TWITCH_CLIENT_ID=your-application-client-id
TWITCH_USER_ACCESS_TOKEN=your-user-access-token
TWITCH_USER_ID=the-token-owner-numeric-id
TWITCH_CHANNELS=rockstargames,another_channel
# TWITCH_CHANNEL_IDS=123456,234567
```

For example: `set -a; source .env; set +a`. Never enable shell tracing while secrets are loaded.

On startup ClipHunter calls Twitch `/validate` and refuses a token with the wrong Client ID, wrong user ID, missing scope, or less than the configured minimum lifetime. It revalidates hourly while messages are flowing. Credentials are excluded from object representations, diagnostic output and handled HTTP errors.

Official references:

- https://dev.twitch.tv/docs/eventsub/handling-websocket-events
- https://dev.twitch.tv/docs/eventsub/reference/chat#channel-chat-message
- https://dev.twitch.tv/docs/eventsub/manage-subscriptions
- https://dev.twitch.tv/docs/authentication/register-app
- https://dev.twitch.tv/docs/authentication/getting-tokens-oauth
- https://dev.twitch.tv/docs/authentication/validate-tokens
- https://dev.twitch.tv/docs/cli/token-command
- https://dev.twitch.tv/docs/api/reference#get-users

Token acquisition and refresh are not automated. The current process expects a valid user token in its environment and never logs it.

### Run

```bash
PYTHONPATH=src python3 -m cliphunter live --database data/cliphunter.db
```

Candidates are written to the same SQLite store used by replay and can be inspected by running the API separately. `SIGINT` and `SIGTERM` trigger graceful shutdown.

The connection automatically recreates subscriptions after unexpected disconnects using bounded exponential backoff. Twitch-directed reconnect messages switch to the supplied reconnect URL and retain subscriptions. Missing keepalives are treated as a failed connection. EventSub message IDs suppress at-least-once duplicate deliveries.

For the first live test, run the diagnostic for 60 seconds while viewing configured channels:

```bash
PYTHONPATH=src python3 -m cliphunter twitch-diagnose \
  --database data/cliphunter.db --duration 60 --interval 5
```

Each JSON line reports `websocket_status`, `websocket_connected`, `active_subscriptions`, `received_chat_messages`, `detected_highlight_candidates`, reconnect/failure counts and invalid messages. It never prints credentials. Use `--duration 0` to run until `SIGINT` or `SIGTERM`.

### Local testing

No Twitch credentials or network access are used by the test suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_twitch.py tests/test_live.py
```

The mocked suite covers normalization, emotes, multiple channels, subscription bodies, duplicate delivery, invalid payloads, keepalive timeout, unexpected disconnects, exponential backoff, Twitch-directed reconnect and graceful shutdown.

## Media worker

Media capture is opt-in and runs as a separate process. It uses the installed `yt-dlp` executable only to resolve an accessible Twitch playback URL, then FFmpeg maintains 2-second segments for 120 seconds. The worker polls the existing SQLite candidate store, writes validated source clips under `data/clips`, then automatically renders `clean_gaming` vertical outputs under `data/rendered/clean_gaming`.

Start chat detection in one terminal, then explicitly start one media worker in another. The numeric broadcaster ID must match the `streamer_id` stored on live candidates:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m cliphunter media-worker \
  CHANNEL_LOGIN BROADCASTER_NUMERIC_ID \
  --database data/cliphunter.db \
  --buffer-dir data/buffer/CHANNEL_LOGIN \
  --clips-dir data/clips/CHANNEL_LOGIN \
  --rendered-dir data/rendered/CHANNEL_LOGIN \
  --retention 120 --segment-seconds 2
```

Run this only for streams you are authorized to capture. The resolver does not load cookies or bypass authentication, DRM, subscriber restrictions or geographic restrictions. Stop with `Ctrl+C`; FFmpeg is terminated and pending extraction tasks are cancelled cleanly.

Render an existing MP4 manually without changing the source:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m cliphunter render \
  data/clips/INPUT.mp4 data/rendered/clean_gaming/OUTPUT.mp4 \
  --preset clean_gaming
```

## Telegram review

Telegram is disabled unless all three variables are configured. Create a bot with BotFather, add it to the target private chat/group, and list only numeric Telegram user IDs permitted to decide reviews:

```ini
TELEGRAM_BOT_TOKEN=local-secret-value
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

Keep these values in the git-ignored, mode-`600` `.env`, then load them without shell tracing:

```bash
set -a; source .env; set +a
```

The media worker creates a persistent pending review and sends each rendered clip once. Run the callback poller separately to process Approve/Reject buttons:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m cliphunter telegram-review \
  --database data/cliphunter.db
```

The poller accepts decisions only from `TELEGRAM_ALLOWED_USER_IDS` in `TELEGRAM_CHAT_ID`. Decisions never publish or delete either video artifact.

Approve finalizes immediately. Reject opens a second menu with `advertisement`, `boring`, `wrong_timing`, and `duplicate`; the selected machine-readable reason is stored with the rejected review.

## Shadow reaction detection

Live commands also run `chat-reaction-shadow-v1` alongside the unchanged production detector. It recognizes configurable laughter, surprise, excitement and funny-failure waves, but writes only to `shadow_candidates`; it never creates clips or Telegram reviews.

Compare recent production and shadow results, including category signal counts:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m cliphunter shadow-report \
  --database data/cliphunter.db --limit 20
```

Filter one numeric broadcaster with `--streamer-id BROADCASTER_ID`. Shadow thresholds are documented in `.env.example` and do not alter `chat-adaptive-v1` settings.
