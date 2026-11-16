"""Live chat buffering that reuses the deterministic offline detector."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any

from .config import DetectionConfig
from .detection import detect_candidates
from .models import Candidate, ChatMessage
from .persistence import CandidateStore
from .twitch import TwitchEventSubClient


class LiveDetectionBuffer:
    """Bound live memory while delegating all scoring to detect_candidates."""

    def __init__(
        self,
        store: CandidateStore,
        config: DetectionConfig | None = None,
        *,
        retention_seconds: float = 600.0,
        remembered_candidates: int = 10_000,
    ) -> None:
        if remembered_candidates < 1:
            raise ValueError("remembered_candidates must be positive")
        self.store = store
        self.config = config or DetectionConfig()
        minimum = (
            self.config.baseline_windows * self.config.stride_seconds
            + self.config.window_seconds
            + self.config.cooldown_seconds
        )
        if retention_seconds < minimum:
            raise ValueError(f"live retention must be at least {minimum:g} seconds")
        self.retention_seconds = retention_seconds
        self.messages: dict[str, deque[ChatMessage]] = defaultdict(deque)
        self._known_order: deque[str] = deque()
        self._known_ids: set[str] = set()
        self._remembered_candidates = remembered_candidates
        self.detected_candidates = 0

    def add(self, message: ChatMessage) -> None:
        channel = self.messages[message.streamer_id]
        channel.append(message)
        cutoff = message.timestamp - self.retention_seconds
        while channel and channel[0].timestamp < cutoff:
            channel.popleft()

    def evaluate(self) -> list[Candidate]:
        snapshot = [message for channel in self.messages.values() for message in channel]
        candidates = detect_candidates(snapshot, self.config)
        self.store.save_many(candidates)
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
