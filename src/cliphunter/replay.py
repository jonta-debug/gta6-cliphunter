"""Strict JSONL reader for deterministic offline Twitch chat replay."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import ChatEmote, ChatMessage


class ReplayFormatError(ValueError):
    """Raised when a JSONL record cannot be converted to a chat message."""

    def __init__(self, path: Path, line_number: int, reason: str) -> None:
        super().__init__(f"{path}:{line_number}: {reason}")
        self.path = path
        self.line_number = line_number
        self.reason = reason


def _timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timestamp must be a number or ISO-8601 string")
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("timestamp must be finite")
        return result
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp cannot be empty")
        try:
            result = float(text)
            if not math.isfinite(result):
                raise ValueError("timestamp must be finite")
            return result
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError as error:
                raise ValueError("timestamp must be a number or ISO-8601 string") from error
    raise ValueError("timestamp must be a number or ISO-8601 string")


def _emote_count(record: dict[str, Any]) -> int:
    if "emotes" in record:
        value = record["emotes"]
    elif "emote_count" in record:
        value = record["emote_count"]
    else:
        value = record.get("is_emote", False)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    if value in (None, ""):
        return 0
    raise ValueError("emotes must be a non-negative integer or list")


def parse_timestamp(value: Any) -> float:
    """Parse numeric or ISO-8601 timestamps consistently as UTC seconds."""
    return _timestamp(value)


def _emotes(record: dict[str, Any]) -> tuple[ChatEmote, ...]:
    value = record.get("emotes")
    if not isinstance(value, list):
        return ()
    found = []
    for item in value:
        if isinstance(item, str):
            found.append(ChatEmote(id=item, text=item))
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            formats = item.get("formats") or item.get("format") or []
            found.append(ChatEmote(
                id=item["id"],
                text=str(item.get("text") or ""),
                emote_set_id=str(item.get("emote_set_id") or ""),
                owner_id=str(item.get("owner_id") or ""),
                formats=tuple(str(value) for value in formats if isinstance(value, str)),
            ))
    return tuple(found)


def read_jsonl(paths: str | Path | Iterable[str | Path]) -> list[ChatMessage]:
    """Read one or more JSONL files and return a stable timestamp-ordered replay."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    messages: list[ChatMessage] = []
    source_order = 0
    for raw_path in paths:
        path = Path(raw_path)
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("record must be a JSON object")
                    streamer_id = record.get("streamer_id")
                    username = record.get("username", record.get("user"))
                    text = record.get("text")
                    if not isinstance(streamer_id, str) or not streamer_id.strip():
                        raise ValueError("streamer_id must be a non-empty string")
                    if not isinstance(username, str) or not username.strip():
                        raise ValueError("username must be a non-empty string")
                    if not isinstance(text, str):
                        raise ValueError("text must be a string")
                    timestamp = _timestamp(record.get("timestamp"))
                    emotes = _emote_count(record)
                    emote_metadata = _emotes(record)
                    attributes = record.get("attributes") or {}
                    if not isinstance(attributes, dict):
                        raise ValueError("attributes must be a JSON object")
                except (json.JSONDecodeError, ValueError) as error:
                    raise ReplayFormatError(path, line_number, str(error)) from error
                source_order += 1
                messages.append(ChatMessage(
                    streamer_id=streamer_id.strip(),
                    timestamp=timestamp,
                    username=username.strip(),
                    text=text,
                    emote_count=emotes,
                    source_line=source_order,
                    user_id=str(record.get("user_id") or ""),
                    event_id=str(record.get("event_id") or record.get("message_id") or ""),
                    emotes=emote_metadata,
                    attributes=attributes,
                ))
    return sorted(messages, key=lambda message: (
        message.timestamp, message.streamer_id, message.source_line
    ))
