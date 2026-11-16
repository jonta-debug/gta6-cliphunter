"""Command-line entry points for replay and local API serving."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Sequence

from .config import DetectionConfig, database_path
from .detection import detect_candidates
from .persistence import SQLiteCandidateStore
from .replay import read_jsonl


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cliphunter")
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser("replay", help="detect candidates from Twitch chat JSONL")
    replay.add_argument("paths", nargs="+", help="one or more JSONL files")
    replay.add_argument("--database", default=database_path(), help="SQLite database path")

    serve = commands.add_parser("serve", help="serve the read-only candidate API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)

    live = commands.add_parser("live", help="monitor configured Twitch chats through EventSub")
    live.add_argument("--database", default=database_path(), help="SQLite database path")

    diagnose = commands.add_parser(
        "twitch-diagnose", help="run live Twitch monitoring with redacted status output"
    )
    diagnose.add_argument("--database", default=database_path(), help="SQLite database path")
    diagnose.add_argument(
        "--duration", type=float, default=60.0,
        help="seconds to run; use 0 to run until interrupted",
    )
    diagnose.add_argument(
        "--interval", type=float, default=5.0, help="status output interval in seconds"
    )

    media_worker = commands.add_parser(
        "media-worker", help="buffer one Twitch stream and extract persisted candidates"
    )
    media_worker.add_argument("channel", help="Twitch channel login")
    media_worker.add_argument("streamer_id", help="numeric broadcaster ID used by candidates")
    media_worker.add_argument("--database", default=database_path(), help="SQLite database path")
    media_worker.add_argument("--buffer-dir", default="./data/buffer")
    media_worker.add_argument("--clips-dir", default="./data/clips")
    media_worker.add_argument("--rendered-dir", default="./data/rendered")
    media_worker.add_argument("--render-preset", default="clean_gaming")
    media_worker.add_argument("--retention", type=float, default=120.0)
    media_worker.add_argument("--segment-seconds", type=float, default=2.0)

    render = commands.add_parser("render", help="render one extracted clip vertically")
    render.add_argument("input", help="source MP4 clip")
    render.add_argument("output", help="separate rendered MP4 path")
    render.add_argument("--preset", default="clean_gaming")

    telegram_review = commands.add_parser(
        "telegram-review", help="poll Telegram for authorized review decisions"
    )
    telegram_review.add_argument("--database", default=database_path(), help="SQLite database path")

    run = commands.add_parser("run", help="run the complete ClipHunter pipeline")
    run.add_argument("--database", default=database_path(), help="SQLite database path")
    run.add_argument("--buffer-root", default="./data/buffer")
    run.add_argument("--clips-root", default="./data/clips")
    run.add_argument("--rendered-root", default="./data/rendered")
    run.add_argument("--retention", type=float, default=120.0)
    run.add_argument("--segment-seconds", type=float, default=2.0)

    shadow_report = commands.add_parser(
        "shadow-report", help="compare production and shadow detector candidates"
    )
    shadow_report.add_argument("--database", default=database_path(), help="SQLite database path")
    shadow_report.add_argument("--streamer-id")
    shadow_report.add_argument("--limit", type=int, default=20)
    return parser


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        event = getattr(signal, name, None)
        if event is not None:
            try:
                loop.add_signal_handler(event, stop_event.set)
            except NotImplementedError:
                pass


async def _build_live_monitor(database: str):
    from .live import LiveChatMonitor, LiveDetectionBuffer
    from .reaction import ReactionDetector, ReactionDetectorConfig
    from .shadow import SQLiteShadowCandidateStore
    from .twitch import TwitchConfig, TwitchEventSubClient, TwitchIdentityClient

    raw_config = TwitchConfig.from_env()
    identity = TwitchIdentityClient(raw_config)
    twitch_config = await identity.prepare()
    detector = LiveDetectionBuffer(
        SQLiteCandidateStore(database), DetectionConfig.from_env(),
        shadow_detector=ReactionDetector(ReactionDetectorConfig.from_env()),
        shadow_store=SQLiteShadowCandidateStore(database),
    )
    client = TwitchEventSubClient(twitch_config, token_validator=identity.validate)
    return LiveChatMonitor(client, detector), client, detector


async def _run_live(database: str) -> None:
    stop_event = asyncio.Event()
    _install_stop_signals(stop_event)
    monitor, _, _ = await _build_live_monitor(database)
    await monitor.run(stop_event)


async def _run_twitch_diagnostic(database: str, duration: float, interval: float) -> None:
    from .live import diagnostic_snapshot

    if duration < 0 or interval <= 0:
        raise ValueError("diagnostic duration must be non-negative and interval must be positive")
    stop_event = asyncio.Event()
    _install_stop_signals(stop_event)
    monitor, client, detector = await _build_live_monitor(database)
    monitor_task = asyncio.create_task(monitor.run(stop_event))
    deadline = asyncio.get_running_loop().time() + duration if duration else None
    try:
        while not stop_event.is_set() and not monitor_task.done():
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                break
            wait_seconds = interval if remaining is None else min(interval, remaining)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
            print(json.dumps(diagnostic_snapshot(client, detector), sort_keys=True), flush=True)
        if monitor_task.done():
            await monitor_task
    finally:
        stop_event.set()
        if not monitor_task.done():
            await monitor_task
        print(json.dumps(diagnostic_snapshot(client, detector), sort_keys=True), flush=True)


async def _run_media_worker(args) -> None:
    from .media import YtDlpTwitchResolver
    from .media_worker import MediaWorker
    from .renderer import VerticalRenderer
    from .reviews import SQLiteReviewStore
    from .rolling_buffer import RollingBuffer
    from .telegram import ReviewCoordinator, TelegramBotAPI, TelegramConfig

    stop_event = asyncio.Event()
    _install_stop_signals(stop_event)
    source = await YtDlpTwitchResolver().resolve(args.channel)
    buffer = RollingBuffer(
        source,
        args.buffer_dir,
        retention_seconds=args.retention,
        segment_seconds=args.segment_seconds,
    )
    telegram_config = TelegramConfig.from_env_optional()
    review_coordinator = ReviewCoordinator(
        SQLiteReviewStore(args.database),
        TelegramBotAPI(telegram_config) if telegram_config else None,
    )
    worker = MediaWorker(
        buffer,
        SQLiteCandidateStore(args.database),
        args.streamer_id,
        args.clips_dir,
        renderer=VerticalRenderer(),
        rendered_directory=args.rendered_dir,
        render_preset=args.render_preset,
        review_coordinator=review_coordinator,
        streamer_label=args.channel,
    )
    await worker.run(stop_event)


async def _run_render(args) -> None:
    from .renderer import VerticalRenderer

    await VerticalRenderer().render(args.input, args.output, preset=args.preset)


async def _run_telegram_review(database: str) -> None:
    from .reviews import SQLiteReviewStore
    from .telegram import TelegramBotAPI, TelegramCallbackWorker, TelegramConfig

    config = TelegramConfig.from_env_optional()
    if config is None:
        raise ValueError("Telegram is disabled; configure all TELEGRAM_* variables")
    stop_event = asyncio.Event()
    _install_stop_signals(stop_event)
    await TelegramCallbackWorker(
        config, TelegramBotAPI(config), SQLiteReviewStore(database)
    ).run(stop_event)


async def _run_pipeline(args) -> None:
    from .orchestrator import RunConfig, build_pipeline

    stop_event = asyncio.Event()
    _install_stop_signals(stop_event)
    runner, channels, telegram_enabled = await build_pipeline(RunConfig(
        database=args.database,
        buffer_root=args.buffer_root,
        clips_root=args.clips_root,
        rendered_root=args.rendered_root,
        retention_seconds=args.retention,
        segment_seconds=args.segment_seconds,
    ))
    logging.getLogger(__name__).info(
        "ClipHunter started: channels=%s telegram=%s database=%s",
        ",".join(channels),
        "enabled" if telegram_enabled else "disabled",
        args.database,
    )
    await runner.run(stop_event)
    logging.getLogger(__name__).info("ClipHunter stopped cleanly")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "replay":
        messages = read_jsonl(args.paths)
        candidates = detect_candidates(messages, DetectionConfig.from_env())
        SQLiteCandidateStore(args.database).save_many(candidates)
        print(json.dumps({
            "messages": len(messages),
            "candidates": len(candidates),
            "candidate_ids": [candidate.id for candidate in candidates],
            "database": args.database,
        }, sort_keys=True))
        return 0

    if args.command == "shadow-report":
        from .shadow import SQLiteShadowCandidateStore
        if args.limit < 1:
            raise SystemExit("Shadow report error: limit must be positive")
        production = SQLiteCandidateStore(args.database).list(
            streamer_id=args.streamer_id, limit=args.limit
        )
        shadow = SQLiteShadowCandidateStore(args.database).list(
            streamer_id=args.streamer_id, limit=args.limit
        )
        print(json.dumps({
            "production_candidates": [item.to_dict() for item in production],
            "shadow_candidates": [item.to_dict() for item in shadow],
        }, sort_keys=True))
        return 0

    if args.command in {"live", "twitch-diagnose"}:
        from .twitch import TwitchAuthenticationError, TwitchConfigurationError, TwitchEventSubError
        try:
            if args.command == "live":
                asyncio.run(_run_live(args.database))
            else:
                asyncio.run(_run_twitch_diagnostic(args.database, args.duration, args.interval))
        except (TwitchConfigurationError, ValueError) as error:
            raise SystemExit(f"Twitch configuration error: {error}") from None
        except TwitchAuthenticationError as error:
            raise SystemExit(f"Twitch authentication error: {error}") from None
        except TwitchEventSubError as error:
            raise SystemExit(f"Twitch connection error: {error}") from None
        return 0

    if args.command == "media-worker":
        from .media import MediaSourceError
        from .rolling_buffer import RollingBufferError
        try:
            asyncio.run(_run_media_worker(args))
        except (MediaSourceError, RollingBufferError, ValueError) as error:
            raise SystemExit(f"Media worker error: {error}") from None
        return 0

    if args.command == "render":
        from .renderer import RenderError
        try:
            asyncio.run(_run_render(args))
        except (RenderError, OSError, ValueError) as error:
            raise SystemExit(f"Render error: {error}") from None
        return 0

    if args.command == "telegram-review":
        from .telegram import TelegramAPIError, TelegramConfigurationError
        try:
            asyncio.run(_run_telegram_review(args.database))
        except (TelegramAPIError, TelegramConfigurationError, ValueError) as error:
            raise SystemExit(f"Telegram review error: {error}") from None
        return 0

    if args.command == "run":
        from .telegram import TelegramConfigurationError
        from .twitch import TwitchAuthenticationError, TwitchConfigurationError, TwitchEventSubError
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            asyncio.run(_run_pipeline(args))
        except (TwitchConfigurationError, TwitchAuthenticationError, TwitchEventSubError,
                TelegramConfigurationError, ValueError) as error:
            raise SystemExit(f"Pipeline startup error: {error}") from None
        return 0

    import uvicorn
    from .api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0
