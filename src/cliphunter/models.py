"""Domain records shared by replay, detection, persistence, and API layers."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass(frozen=True)
class ChatEmote:
    id: str
    text: str = ""
    emote_set_id: str = ""
    owner_id: str = ""
    formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatMessage:
    streamer_id: str
    timestamp: float
    username: str
    text: str
    emote_count: int = 0
    source_line: int = 0
    user_id: str = ""
    event_id: str = ""
    emotes: tuple[ChatEmote, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalWindow:
    streamer_id: str
    start: float
    end: float
    message_count: int
    unique_participants: int
    emote_ratio: float
    caps_ratio: float


@dataclass(frozen=True)
class Candidate:
    id: str
    streamer_id: str
    event_timestamp: float
    hype_score: float
    signals: dict[str, Any]
    clip_start: float
    clip_end: float
    detector_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "streamer_id": self.streamer_id,
            "event_timestamp": self.event_timestamp,
            "hype_score": self.hype_score,
            "signals": self.signals,
            "clip_start": self.clip_start,
            "clip_end": self.clip_end,
            "detector_version": self.detector_version,
        }
