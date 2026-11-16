"""Isolated worker that turns persisted highlight candidates into local clips."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from .models import Candidate
from .persistence import CandidateStore
from .renderer import RenderError, VerticalRenderer
from .telegram import ReviewCoordinator, ReviewDeliveryError
from .rolling_buffer import RollingBuffer, RollingBufferError

log = logging.getLogger(__name__)


class MediaWorker:
    def __init__(
        self,
        buffer: RollingBuffer,
        store: CandidateStore,
        streamer_id: str,
        clips_directory: str | Path,
        *,
        renderer: VerticalRenderer | None = None,
        rendered_directory: str | Path | None = None,
        render_preset: str = "clean_gaming",
        review_coordinator: ReviewCoordinator | None = None,
        streamer_label: str | None = None,
        poll_interval_seconds: float = 1.0,
        clip_wait_timeout_seconds: float = 90.0,
        shutdown_drain_timeout_seconds: float = 15.0,
    ) -> None:
        if (
            not streamer_id
            or poll_interval_seconds <= 0
            or clip_wait_timeout_seconds <= 0
            or shutdown_drain_timeout_seconds < 0
        ):
            raise ValueError("invalid media worker configuration")
        self.buffer = buffer
        self.store = store
        self.streamer_id = streamer_id
        self.clips_directory = Path(clips_directory)
        self.renderer = renderer
        self.rendered_directory = Path(rendered_directory) if rendered_directory else None
        self.render_preset = render_preset
        self.review_coordinator = review_coordinator
        self.streamer_label = streamer_label or streamer_id
        if self.renderer is not None and self.rendered_directory is None:
            raise ValueError("rendered_directory is required when rendering is enabled")
        self.poll_interval_seconds = poll_interval_seconds
        self.clip_wait_timeout_seconds = clip_wait_timeout_seconds
        self.shutdown_drain_timeout_seconds = shutdown_drain_timeout_seconds
        self._handled: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.render_errors: dict[str, str] = {}
        self.review_errors: dict[str, str] = {}

    async def _extract(self, candidate: Candidate) -> None:
        output = self.clips_directory / f"{candidate.id}.mp4"
        try:
            log.info(
                "processing candidate %s for streamer %s (clip %.3f..%.3f)",
                candidate.id,
                candidate.streamer_id,
                candidate.clip_start,
                candidate.clip_end,
            )
            if output.exists():
                try:
                    await self.buffer.validate_clip(output)
                except RollingBufferError:
                    await self.buffer.extract_clip(
                        candidate.id,
                        candidate.clip_start,
                        candidate.clip_end,
                        output,
                        wait_timeout=self.clip_wait_timeout_seconds,
                    )
            else:
                await self.buffer.extract_clip(
                    candidate.id,
                    candidate.clip_start,
                    candidate.clip_end,
                    output,
                    wait_timeout=self.clip_wait_timeout_seconds,
                )
        except (RollingBufferError, OSError) as error:
            self.errors[candidate.id] = str(error)
            log.error("clip extraction failed for candidate %s: %s", candidate.id, error)
            return
        log.info("clip extraction completed for candidate %s: %s", candidate.id, output)
        if self.renderer is None or self.rendered_directory is None:
            return
        rendered = self.rendered_directory / self.render_preset / f"{candidate.id}.mp4"
        try:
            if rendered.exists():
                try:
                    await self.renderer.validate(output, rendered, preset=self.render_preset)
                except RenderError:
                    await self.renderer.render(output, rendered, preset=self.render_preset)
            else:
                await self.renderer.render(output, rendered, preset=self.render_preset)
        except (RenderError, OSError) as error:
            self.render_errors[candidate.id] = str(error)
            log.error("vertical rendering failed for candidate %s: %s", candidate.id, error)
            return
        log.info("vertical rendering completed for candidate %s: %s", candidate.id, rendered)
        if self.review_coordinator is None:
            return
        try:
            await self.review_coordinator.complete(
                candidate,
                streamer_label=self.streamer_label,
                original_path=output,
                rendered_path=rendered,
            )
            log.info("review handoff completed for candidate %s", candidate.id)
        except (ReviewDeliveryError, sqlite3.Error, OSError, ValueError) as error:
            self.review_errors[candidate.id] = str(error)
            log.error("review delivery failed for candidate %s: %s", candidate.id, error)

    def _start_new_candidates(self) -> None:
        candidates = self.store.list(streamer_id=self.streamer_id, limit=100)
        for candidate in candidates:
            if candidate.id in self._handled:
                continue
            self._handled.add(candidate.id)
            log.info("queued persisted candidate %s for media processing", candidate.id)
            self._tasks[candidate.id] = asyncio.create_task(self._extract(candidate))

    def _collect_finished(self) -> None:
        for candidate_id, task in list(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(candidate_id)
            if not task.cancelled():
                task.result()

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        self.clips_directory.mkdir(parents=True, exist_ok=True)
        await self.buffer.start()
        try:
            while not stop_event.is_set():
                self._start_new_candidates()
                self._collect_finished()
                if self.buffer.last_error:
                    raise RollingBufferError(self.buffer.last_error)
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            # Let the chat monitor's synchronous final evaluation run, then pick up
            # any candidate it persisted before draining work with capture active.
            await asyncio.sleep(0)
            self._start_new_candidates()
            if self._tasks and self.shutdown_drain_timeout_seconds:
                _, pending = await asyncio.wait(
                    self._tasks.values(),
                    timeout=self.shutdown_drain_timeout_seconds,
                )
            else:
                pending = {task for task in self._tasks.values() if not task.done()}
            await asyncio.gather(
                *(task for task in self._tasks.values() if task.done()),
                return_exceptions=True,
            )
            if pending:
                log.warning(
                    "cancelling %d in-flight media task(s) during shutdown: %s",
                    len(pending),
                    ", ".join(
                        sorted(
                            candidate_id
                            for candidate_id, task in self._tasks.items()
                            if task in pending
                        )
                    ),
                )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._tasks.clear()
            await self.buffer.stop()
