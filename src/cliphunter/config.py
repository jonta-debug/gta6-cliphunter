"""Typed configuration for offline chat detection and local persistence."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoreWeights:
    frequency: float = 0.32
    relative_activity: float = 0.28
    participants: float = 0.22
    emotes: float = 0.10
    caps: float = 0.08

    def normalized(self) -> dict[str, float]:
        values = {
            "frequency": self.frequency,
            "relative_activity": self.relative_activity,
            "participants": self.participants,
            "emotes": self.emotes,
            "caps": self.caps,
        }
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("score weights must be finite and non-negative")
        total = sum(values.values())
        if total <= 0:
            raise ValueError("at least one score weight must be positive")
        return {name: value / total for name, value in values.items()}


@dataclass(frozen=True)
class DetectionConfig:
    window_seconds: float = 10.0
    stride_seconds: float = 5.0
    baseline_windows: int = 24
    min_baseline_windows: int = 6
    hype_threshold: float = 5.5
    cooldown_seconds: float = 45.0
    clip_pre_roll_seconds: float = 30.0
    clip_post_roll_seconds: float = 20.0
    surprise_scale: float = 3.0
    relative_full_scale: float = 4.0
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    def __post_init__(self) -> None:
        numeric_values = (
            self.window_seconds,
            self.stride_seconds,
            self.hype_threshold,
            self.cooldown_seconds,
            self.clip_pre_roll_seconds,
            self.clip_post_roll_seconds,
            self.surprise_scale,
            self.relative_full_scale,
        )
        if any(not math.isfinite(value) for value in numeric_values):
            raise ValueError("detector settings must be finite")
        if self.window_seconds <= 0 or self.stride_seconds <= 0:
            raise ValueError("window and stride must be positive")
        if self.stride_seconds > self.window_seconds:
            raise ValueError("stride cannot exceed window size")
        if self.baseline_windows < 1:
            raise ValueError("baseline_windows must be positive")
        if not 1 <= self.min_baseline_windows <= self.baseline_windows:
            raise ValueError("min_baseline_windows must be within baseline history")
        if not 0 <= self.hype_threshold <= 10:
            raise ValueError("hype_threshold must be between 0 and 10")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown cannot be negative")
        if self.clip_pre_roll_seconds < 0 or self.clip_post_roll_seconds < 0:
            raise ValueError("clip roll durations cannot be negative")
        if self.surprise_scale <= 0 or self.relative_full_scale <= 1:
            raise ValueError("score scales are invalid")
        self.weights.normalized()

    @classmethod
    def from_env(cls) -> "DetectionConfig":
        env = os.environ.get
        return cls(
            window_seconds=float(env("CLIPHUNTER_WINDOW_SECONDS", "10")),
            stride_seconds=float(env("CLIPHUNTER_STRIDE_SECONDS", "5")),
            baseline_windows=int(env("CLIPHUNTER_BASELINE_WINDOWS", "24")),
            min_baseline_windows=int(env("CLIPHUNTER_MIN_BASELINE_WINDOWS", "6")),
            hype_threshold=float(env("CLIPHUNTER_HYPE_THRESHOLD", "5.5")),
            cooldown_seconds=float(env("CLIPHUNTER_COOLDOWN_SECONDS", "45")),
            clip_pre_roll_seconds=float(env("CLIPHUNTER_CLIP_PRE_ROLL_SECONDS", "30")),
            clip_post_roll_seconds=float(env("CLIPHUNTER_CLIP_POST_ROLL_SECONDS", "20")),
            surprise_scale=float(env("CLIPHUNTER_SURPRISE_SCALE", "3")),
            relative_full_scale=float(env("CLIPHUNTER_RELATIVE_FULL_SCALE", "4")),
            weights=ScoreWeights(
                frequency=float(env("CLIPHUNTER_WEIGHT_FREQUENCY", "0.32")),
                relative_activity=float(env("CLIPHUNTER_WEIGHT_RELATIVE_ACTIVITY", "0.28")),
                participants=float(env("CLIPHUNTER_WEIGHT_PARTICIPANTS", "0.22")),
                emotes=float(env("CLIPHUNTER_WEIGHT_EMOTES", "0.10")),
                caps=float(env("CLIPHUNTER_WEIGHT_CAPS", "0.08")),
            ),
        )


def database_path() -> str:
    return os.environ.get("CLIPHUNTER_DATABASE_PATH", "./data/cliphunter.db")
