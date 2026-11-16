"""Live chat buffering that reuses the deterministic offline detector."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Any

from .config import DetectionConfig
from .detection import detect_candidates
from .models import Candidate, ChatMessage
from .persistence import CandidateStore
from .reaction import ReactionDetector
from .shadow import SQLiteShadowCandidateStore
from .twitch import TwitchEventSubClient

log = logging.getLogger(__name__)


class LiveDetectionBuffer:
    """Bound live memory while delegating all scoring to detect_candidates."""

    def __init__(
        self,
        store: CandidateStore,
        config: DetectionConfig | None = None,
        *,
        shadow_detector: ReactionDetector | None = None,
        shadow_store: SQLiteShadowCandidateStore | None = None,
        retention_seconds: float = 600.0,
        media_retention_seconds: float | None = None,
        remembered_candidates: int = 10_000,
    ) -> None:
        if remembered_candidates < 1:
            raise ValueError("remembered_candidates must be positive")
        self.store = store
        self.config = config or DetectionConfig()
        self.shadow_detector = shadow_detector
        self.shadow_store = shadow_store
        if (shadow_detector is None) != (shadow_store is None):
            raise ValueError("shadow detector and store must be configured together")
        minimum = (
            self.config.baseline_windows * self.config.stride_seconds
            + self.config.window_seconds
            + self.config.cooldown_seconds
        )
        if retention_seconds < minimum:
            raise ValueError(f"live retention must be at least {minimum:g} seconds")
        self.retention_seconds = retention_seconds
        if media_retention_seconds is not None and media_retention_seconds <= 0:
            raise ValueError("media retention must be positive")
        self.media_retention_seconds = media_retention_seconds
        self.messages: dict[str, deque[ChatMessage]] = defaultdict(deque)
        self._known_order: deque[str] = deque()
        self._known_ids: set[str] = set()
        self._remembered_candidates = remembered_candidates
        self.detected_candidates = 0
        existing_shadow = shadow_store.list(limit=10_000) if shadow_store else []
        self._shadow_known_ids = {candidate.id for candidate in existing_shadow}
        self.shadow_candidates = len(self._shadow_known_ids)
        self.shadow_failures = 0

    def add(self, message: ChatMessage) -> None:
        channel = self.messages[message.streamer_id]
        channel.append(message)
        cutoff = message.timestamp - self.retention_seconds
        while channel and channel[0].timestamp < cutoff:
            channel.popleft()

    def evaluate(self) -> list[Candidate]:
        snapshot = [message for channel in self.messages.values() for message in channel]
        candidates = detect_candidates(snapshot, self.config)
        if self.media_retention_seconds is not None and snapshot:
            newest_by_streamer: dict[str, float] = {}
            for message in snapshot:
                newest_by_streamer[message.streamer_id] = max(
                    message.timestamp,
                    newest_by_streamer.get(message.streamer_id, message.timestamp),
                )
            processable = []
            for candidate in candidates:
                media_cutoff = (
                    newest_by_streamer[candidate.streamer_id]
                    - self.media_retention_seconds
                )
                if candidate.clip_start < media_cutoff:
                    log.warning(
                        "discarding stale live candidate %s: clip start %.3f is "
                        "older than media cutoff %.3f",
                        candidate.id,
                        candidate.clip_start,
                        media_cutoff,
                    )
                    continue
                processable.append(candidate)
            candidates = processable
        self.store.save_many(candidates)
        if self.shadow_detector is not None and self.shadow_store is not None:
            try:
                shadow = self.shadow_detector.detect(snapshot)
                self.shadow_store.save_many(shadow)
                self._shadow_known_ids.update(candidate.id for candidate in shadow)
                self.shadow_candidates = len(self._shadow_known_ids)
            except Exception as error:
                self.shadow_failures += 1
                log.error("shadow reaction evaluation failed: %s", error)
        new = []
        for candidate in candidates:
            if candidate.id in self._known_ids:
                continue
            self._known_ids.add(candidate.id)
            self._known_order.append(candidate.id)
            new.append(candidate)
        while len(self._known_order) > self._remembered_candidates:
            self._known_ids.discard(self._known_order.popleft())
        self.detected_candidates += len(new)
        return new


def diagnostic_snapshot(
    client: TwitchEventSubClient,
    detector: LiveDetectionBuffer,
) -> dict[str, int | str | bool]:
    """Return operational state without configuration or credential values."""
    return {
        "websocket_status": client.health.status,
        "websocket_connected": client.health.connected,
        "active_subscriptions": client.health.subscriptions,
        "received_chat_messages": client.health.received_chat_messages,
        "detected_highlight_candidates": detector.detected_candidates,
        "shadow_reaction_candidates": getattr(detector, "shadow_candidates", 0),
        "shadow_reaction_failures": getattr(detector, "shadow_failures", 0),
        "reconnects": client.health.reconnects,
        "connection_failures": client.health.failures,
        "invalid_messages": client.health.invalid_messages,
    }


class LiveChatMonitor:
    def __init__(
        self,
        client: TwitchEventSubClient,
        detector: LiveDetectionBuffer,
        *,
        evaluation_interval_seconds: float = 5.0,
    ) -> None:
        if evaluation_interval_seconds <= 0:
            raise ValueError("evaluation interval must be positive")
        self.client = client
        self.detector = detector
        self.evaluation_interval_seconds = evaluation_interval_seconds

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        listener = asyncio.create_task(self.client.run(self.detector.add, stop_event))
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.evaluation_interval_seconds)
                except TimeoutError:
                    self.detector.evaluate()
                if listener.done():
                    await listener
                    break
            self.detector.evaluate()
        finally:
            stop_event.set()
            if not listener.done():
                try:
                    await asyncio.wait_for(listener, timeout=2)
                except TimeoutError:
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
