"""Deterministic, persistence-only chat reaction detector for shadow evaluation."""

from __future__ import annotations

import hashlib
import math
import os
import re
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import ChatMessage

REACTION_DETECTOR_VERSION = "chat-reaction-shadow-v1"
REACTION_CATEGORIES = ("laughter", "surprise", "excitement", "funny_failure")


@dataclass(frozen=True)
class ReactionDetectorConfig:
    window_seconds: float = 10.0
    stride_seconds: float = 5.0
    baseline_windows: int = 12
    min_baseline_windows: int = 4
    cooldown_seconds: float = 30.0
    minimum_distinct_users: int = 2
    minimum_effective_reactions: float = 2.0
    minimum_increase: float = 1.0
    relative_lift_threshold: float = 2.0
    full_lift: float = 6.0
    repeat_user_weight: float = 0.2
    score_threshold: float = 4.0
    smoothing: float = 0.5
    category_minimums: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.window_seconds <= 0 or self.stride_seconds <= 0:
            raise ValueError("reaction window and stride must be positive")
        if self.stride_seconds > self.window_seconds:
            raise ValueError("reaction stride cannot exceed its window")
        if not 1 <= self.min_baseline_windows <= self.baseline_windows:
            raise ValueError("invalid reaction baseline window count")
        if self.cooldown_seconds < 0 or self.minimum_distinct_users < 1:
            raise ValueError("invalid reaction grouping settings")
        if self.minimum_effective_reactions <= 0 or self.minimum_increase < 0:
            raise ValueError("invalid reaction thresholds")
        if self.relative_lift_threshold <= 1 or self.full_lift <= self.relative_lift_threshold:
            raise ValueError("invalid reaction lift thresholds")
        if not 0 <= self.repeat_user_weight <= 1:
            raise ValueError("repeat-user weight must be between zero and one")
        if not 0 <= self.score_threshold <= 10 or self.smoothing <= 0:
            raise ValueError("invalid reaction score settings")
        if any(key not in REACTION_CATEGORIES or value <= 0
               for key, value in self.category_minimums.items()):
            raise ValueError("invalid category-specific reaction threshold")

    def category_minimum(self, category: str) -> float:
        return self.category_minimums.get(category, self.minimum_effective_reactions)

    @classmethod
    def from_env(cls) -> "ReactionDetectorConfig":
        env = os.environ.get
        return cls(
            window_seconds=float(env("CLIPHUNTER_REACTION_WINDOW_SECONDS", "10")),
            stride_seconds=float(env("CLIPHUNTER_REACTION_STRIDE_SECONDS", "5")),
            baseline_windows=int(env("CLIPHUNTER_REACTION_BASELINE_WINDOWS", "12")),
            min_baseline_windows=int(env("CLIPHUNTER_REACTION_MIN_BASELINE_WINDOWS", "4")),
            cooldown_seconds=float(env("CLIPHUNTER_REACTION_COOLDOWN_SECONDS", "30")),
            minimum_distinct_users=int(env("CLIPHUNTER_REACTION_MIN_DISTINCT_USERS", "2")),
            minimum_effective_reactions=float(env("CLIPHUNTER_REACTION_MIN_EFFECTIVE", "2")),
            minimum_increase=float(env("CLIPHUNTER_REACTION_MIN_INCREASE", "1")),
            relative_lift_threshold=float(env("CLIPHUNTER_REACTION_LIFT_THRESHOLD", "2")),
            full_lift=float(env("CLIPHUNTER_REACTION_FULL_LIFT", "6")),
            repeat_user_weight=float(env("CLIPHUNTER_REACTION_REPEAT_USER_WEIGHT", "0.2")),
            score_threshold=float(env("CLIPHUNTER_REACTION_SCORE_THRESHOLD", "4")),
        )


@dataclass(frozen=True)
class ShadowCandidate:
    id: str
    streamer_id: str
    event_timestamp: float
    score: float
    window_start: float
    window_end: float
    categories: dict[str, dict[str, Any]]
    detector_version: str = REACTION_DETECTOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "streamer_id": self.streamer_id,
            "event_timestamp": self.event_timestamp,
            "score": self.score,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "reasons": sorted(self.categories),
            "categories": self.categories,
            "detector_version": self.detector_version,
        }


class ReactionMatcher:
    _patterns = {
        "laughter": (
            re.compile(r"(?<!\w)(?:HA){3,}(?!\w)", re.IGNORECASE),
            re.compile(r"(?<!\w)(?:LOL|LMAO|ROFL)(?!\w)", re.IGNORECASE),
        ),
        "surprise": (
            re.compile(r"(?<!\w)(?:WTF|OMG)(?!\w)", re.IGNORECASE),
            re.compile(r"(?<!\w)NO\s+WAY(?!\w)", re.IGNORECASE),
        ),
        "excitement": (
            re.compile(r"(?<!\w)(?:W|LETSGO|INSANE)(?!\w)", re.IGNORECASE),
        ),
        "funny_failure": (
            re.compile(r"(?<!\w)(?:BRUH|L)(?!\w)", re.IGNORECASE),
        ),
    }
    _emoji = {
        "laughter": ("😂", "🤣"),
        "funny_failure": ("💀",),
    }
    _emotes = {
        "laughter": frozenset({"lul", "lulw", "kekw", "omegalul"}),
        "surprise": frozenset({"wutface"}),
        "excitement": frozenset({"pog", "pogchamp", "poggers"}),
        "funny_failure": frozenset({"failfish"}),
    }

    def match(self, message: ChatMessage) -> frozenset[str]:
        categories: set[str] = set()
        for category, patterns in self._patterns.items():
            if any(pattern.search(message.text) for pattern in patterns):
                categories.add(category)
        for category, emoji in self._emoji.items():
            if any(value in message.text for value in emoji):
                categories.add(category)
        emote_names = {emote.text.casefold() for emote in message.emotes if emote.text}
        for category, names in self._emotes.items():
            if emote_names & names:
                categories.add(category)
        return frozenset(categories)


@dataclass(frozen=True)
class _ReactionWindow:
    streamer_id: str
    start: float
    end: float
    message_count: int
    signals: dict[str, dict[str, float | int]]


@dataclass(frozen=True)
class _Detection:
    streamer_id: str
    category: str
    start: float
    end: float
    event_timestamp: float
    score: float
    signals: dict[str, Any]


class ReactionDetector:
    def __init__(
        self,
        config: ReactionDetectorConfig | None = None,
        matcher: ReactionMatcher | None = None,
    ) -> None:
        self.config = config or ReactionDetectorConfig()
        self.matcher = matcher or ReactionMatcher()

    def _windows(self, messages: Iterable[ChatMessage]) -> dict[str, list[_ReactionWindow]]:
        grouped: dict[str, list[tuple[ChatMessage, frozenset[str]]]] = defaultdict(list)
        for message in messages:
            grouped[message.streamer_id].append((message, self.matcher.match(message)))
        output: dict[str, list[_ReactionWindow]] = {}
        for streamer_id, records in grouped.items():
            records.sort(key=lambda item: (item[0].timestamp, item[0].user_id or item[0].username))
            timestamps = [record[0].timestamp for record in records]
            first = math.floor(timestamps[0] / self.config.stride_seconds) * self.config.stride_seconds
            last = math.floor(timestamps[-1] / self.config.stride_seconds) * self.config.stride_seconds
            windows: list[_ReactionWindow] = []
            start = first
            while start <= last:
                end = start + self.config.window_seconds
                left = bisect_left(timestamps, start)
                right = bisect_left(timestamps, end)
                selected = records[left:right]
                signals: dict[str, dict[str, float | int]] = {}
                for category in REACTION_CATEGORIES:
                    reacting = [item[0] for item in selected if category in item[1]]
                    users = Counter(item.user_id or item.username for item in reacting)
                    effective = sum(
                        1.0 + (count - 1) * self.config.repeat_user_weight
                        for count in users.values()
                    )
                    signals[category] = {
                        "reaction_count": len(reacting),
                        "distinct_users": len(users),
                        "effective_count": round(effective, 6),
                        "reaction_rate": round(effective / max(len(selected), 1), 6),
                    }
                windows.append(_ReactionWindow(
                    streamer_id, start, end, len(selected), signals
                ))
                start += self.config.stride_seconds
            output[streamer_id] = windows
        return output

    def detect(self, messages: Iterable[ChatMessage]) -> list[ShadowCandidate]:
        detections: list[_Detection] = []
        for windows in self._windows(messages).values():
            histories = {
                category: deque(maxlen=self.config.baseline_windows)
                for category in REACTION_CATEGORIES
            }
            for window in windows:
                for category in REACTION_CATEGORIES:
                    current = window.signals[category]
                    history = histories[category]
                    if len(history) >= self.config.min_baseline_windows:
                        baseline_count = statistics.median(
                            float(item["effective_count"]) for item in history
                        )
                        baseline_rate = statistics.median(
                            float(item["reaction_rate"]) for item in history
                        )
                        effective = float(current["effective_count"])
                        reaction_rate = float(current["reaction_rate"])
                        count_lift = (
                            (effective + self.config.smoothing)
                            / (baseline_count + self.config.smoothing)
                        )
                        rate_lift = (
                            (reaction_rate + self.config.smoothing / max(window.message_count, 1))
                            / (baseline_rate + self.config.smoothing / max(window.message_count, 1))
                        )
                        lift = max(count_lift, rate_lift)
                        minimum = self.config.category_minimum(category)
                        if effective >= minimum \
                                and int(current["distinct_users"]) >= self.config.minimum_distinct_users \
                                and effective >= baseline_count + self.config.minimum_increase \
                                and lift >= self.config.relative_lift_threshold:
                            volume = min(1.0, effective / (minimum * 2))
                            breadth = min(
                                1.0,
                                int(current["distinct_users"])
                                / (self.config.minimum_distinct_users * 3),
                            )
                            lift_score = min(
                                1.0,
                                (lift - 1.0) / (self.config.full_lift - 1.0),
                            )
                            score = round(10 * (0.35 * volume + 0.30 * breadth + 0.35 * lift_score), 3)
                            if score >= self.config.score_threshold:
                                signals = dict(current)
                                signals.update({
                                    "message_count": window.message_count,
                                    "baseline_effective_count": round(baseline_count, 6),
                                    "baseline_reaction_rate": round(baseline_rate, 6),
                                    "relative_lift": round(lift, 6),
                                    "score": score,
                                })
                                detections.append(_Detection(
                                    window.streamer_id,
                                    category,
                                    window.start,
                                    window.end,
                                    round((window.start + window.end) / 2, 6),
                                    score,
                                    signals,
                                ))
                    history.append(current)
        return self._group(detections)

    def _group(self, detections: list[_Detection]) -> list[ShadowCandidate]:
        detections.sort(key=lambda item: (item.streamer_id, item.event_timestamp, item.category))
        output: list[ShadowCandidate] = []
        cluster: list[_Detection] = []
        cluster_end = 0.0

        def finish() -> None:
            if not cluster:
                return
            strongest = max(cluster, key=lambda item: (item.score, -item.event_timestamp))
            categories: dict[str, dict[str, Any]] = {}
            for item in cluster:
                current = categories.get(item.category)
                if current is None or item.score > float(current["score"]):
                    categories[item.category] = item.signals
            window_start = min(item.start for item in cluster)
            window_end = max(item.end for item in cluster)
            identity = (
                f"{REACTION_DETECTOR_VERSION}|{strongest.streamer_id}|{window_start:.6f}"
            )
            output.append(ShadowCandidate(
                id=hashlib.sha256(identity.encode()).hexdigest()[:24],
                streamer_id=strongest.streamer_id,
                event_timestamp=strongest.event_timestamp,
                score=strongest.score,
                window_start=window_start,
                window_end=window_end,
                categories=categories,
            ))

        for detection in detections:
            same_streamer = bool(cluster) and detection.streamer_id == cluster[0].streamer_id
            related = same_streamer and (
                detection.event_timestamp - cluster[-1].event_timestamp <= self.config.cooldown_seconds
                or detection.start < cluster_end
            )
            if cluster and not related:
                finish()
                cluster = []
                cluster_end = 0.0
            cluster.append(detection)
            cluster_end = max(cluster_end, detection.end)
        finish()
        return output
