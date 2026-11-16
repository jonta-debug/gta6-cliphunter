"""Single-process async supervision for the complete ClipHunter pipeline."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DetectionConfig
from .live import LiveChatMonitor, LiveDetectionBuffer
from .media import MediaSource, MediaSourceResolver, YtDlpTwitchResolver
from .media_worker import MediaWorker
from .persistence import SQLiteCandidateStore
from .renderer import VerticalRenderer
from .reviews import SQLiteReviewStore
from .rolling_buffer import RollingBuffer
from .telegram import (
    ReviewCoordinator,
    TelegramBotAPI,
    TelegramCallbackWorker,
    TelegramConfig,
)
from .twitch import TwitchConfig, TwitchEventSubClient, TwitchIdentityClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineComponent:
    name: str
    run: Callable[[asyncio.Event], Awaitable[None]]
    critical: bool = False


class PipelineRunner:
    def __init__(self, components: list[PipelineComponent]) -> None:
        if not components:
            raise ValueError("pipeline requires at least one component")
        self.components = components

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        tasks = {
            asyncio.create_task(component.run(stop_event), name=component.name): component
            for component in self.components
        }
        stop_task = asyncio.create_task(stop_event.wait(), name="pipeline-stop")
        try:
            while tasks and not stop_event.is_set():
                done, _ = await asyncio.wait(
                    [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    break
                for task in done:
                    component = tasks.pop(task, None)
                    if component is None:
                        continue
                    try:
                        task.result()
                        if not stop_event.is_set():
                            log.error("pipeline component stopped unexpectedly: %s", component.name)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        log.error("pipeline component failed (%s): %s", component.name, error)
                    if component.critical:
                        stop_event.set()
        finally:
            stop_event.set()
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            if tasks:
                pending = list(tasks)
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), timeout=20
                    )
                except TimeoutError:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)


class ChannelMediaSupervisor:
    """Resolve and restart one channel's media worker with bounded backoff."""

    def __init__(
        self,
        channel_login: str,
        resolver: MediaSourceResolver,
        worker_factory: Callable[[MediaSource], MediaWorker],
        *,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 30.0,
    ) -> None:
        self.channel_login = channel_login
        self.resolver = resolver
        self.worker_factory = worker_factory
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds

    async def run(self, stop_event: asyncio.Event) -> None:
        delay = self.backoff_base_seconds
        while not stop_event.is_set():
            try:
                source = await self.resolver.resolve(self.channel_login)
                log.info("media source ready: %s", self.channel_login)
                worker = self.worker_factory(source)
                await worker.run(stop_event)
                if stop_event.is_set():
                    return
                raise RuntimeError("media worker stopped unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("media worker unavailable (%s): %s", self.channel_login, error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                delay = min(self.backoff_max_seconds, max(delay * 2, 0.1))


@dataclass(frozen=True)
class RunConfig:
    database: str
    buffer_root: str = "./data/buffer"
    clips_root: str = "./data/clips"
    rendered_root: str = "./data/rendered"
    retention_seconds: float = 120.0
    segment_seconds: float = 2.0


async def build_pipeline(config: RunConfig) -> tuple[PipelineRunner, tuple[str, ...], bool]:
    raw_twitch = TwitchConfig.from_env()
    identity = TwitchIdentityClient(raw_twitch)
    await identity.validate()
    channels = await identity.resolve_media_channels()
    prepared = replace(
        raw_twitch,
        channel_ids=tuple(channel.user_id for channel in channels),
        channel_logins=(),
    )

    candidate_store = SQLiteCandidateStore(config.database)
    detector = LiveDetectionBuffer(candidate_store, DetectionConfig.from_env())
    twitch_client = TwitchEventSubClient(prepared, token_validator=identity.validate)
    chat_monitor = LiveChatMonitor(twitch_client, detector)
    components = [PipelineComponent("twitch-chat", chat_monitor.run, critical=True)]

    telegram_config = TelegramConfig.from_env_optional()
    review_store = SQLiteReviewStore(config.database)
    telegram_api = TelegramBotAPI(telegram_config) if telegram_config else None
    coordinator = ReviewCoordinator(review_store, telegram_api)
    if telegram_config and telegram_api:
        callback_worker = TelegramCallbackWorker(
            telegram_config, telegram_api, review_store
        )
        components.append(PipelineComponent("telegram-callbacks", callback_worker.run))

    resolver = YtDlpTwitchResolver()
    for channel in channels:
        buffer_directory = Path(config.buffer_root) / channel.login
        clips_directory = Path(config.clips_root) / channel.login
        rendered_directory = Path(config.rendered_root) / channel.login

        def worker_factory(
            source: MediaSource,
            *,
            channel_id: str = channel.user_id,
            channel_login: str = channel.login,
            buffer_dir: Path = buffer_directory,
            clips_dir: Path = clips_directory,
            rendered_dir: Path = rendered_directory,
        ) -> MediaWorker:
            return MediaWorker(
                RollingBuffer(
                    source,
                    buffer_dir,
                    retention_seconds=config.retention_seconds,
                    segment_seconds=config.segment_seconds,
                ),
                candidate_store,
                channel_id,
                clips_dir,
                renderer=VerticalRenderer(),
                rendered_directory=rendered_dir,
                review_coordinator=coordinator,
                streamer_label=channel_login,
            )

        supervisor = ChannelMediaSupervisor(channel.login, resolver, worker_factory)
        components.append(PipelineComponent(f"media:{channel.login}", supervisor.run))

    return PipelineRunner(components), tuple(channel.login for channel in channels), bool(telegram_config)
