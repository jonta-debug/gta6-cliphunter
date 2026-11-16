"""Independent, deterministic adaptive chat-spike detection."""

from __future__ import annotations

import hashlib
import math
import statistics
from collections import defaultdict, deque
from collections.abc import Iterable

from .config import DetectionConfig
from .models import Candidate, ChatMessage, SignalWindow

DETECTOR_VERSION = "chat-adaptive-v1"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _is_caps(text: str) -> bool:
    letters = [character for character in text if character.isalpha()]
    return len(letters) >= 4 and sum(character.isupper() for character in letters) / len(letters) >= 0.8


def _window(streamer_id: str, start: float, end: float, messages: list[ChatMessage]) -> SignalWindow:
    count = len(messages)
    return SignalWindow(
        streamer_id=streamer_id,
        start=start,
        end=end,
        message_count=count,
        unique_participants=len({message.username.casefold() for message in messages}),
        emote_ratio=(sum(message.emote_count > 0 for message in messages) / count) if count else 0.0,
        caps_ratio=(sum(_is_caps(message.text) for message in messages) / count) if count else 0.0,
    )


def build_windows(messages: Iterable[ChatMessage], config: DetectionConfig) -> dict[str, list[SignalWindow]]:
    """Build aligned overlapping windows independently for every streamer."""
    grouped: dict[str, list[ChatMessage]] = defaultdict(list)
    for message in messages:
        grouped[message.streamer_id].append(message)

    result: dict[str, list[SignalWindow]] = {}
    for streamer_id, stream_messages in sorted(grouped.items()):
        stream_messages.sort(key=lambda message: (message.timestamp, message.source_line))
        origin = math.floor(stream_messages[0].timestamp / config.stride_seconds) * config.stride_seconds
        last = stream_messages[-1].timestamp
        windows: list[SignalWindow] = []
        left = right = 0
        start = origin
        while start <= last:
            end = start + config.window_seconds
            while left < len(stream_messages) and stream_messages[left].timestamp < start:
                left += 1
            right = max(right, left)
            while right < len(stream_messages) and stream_messages[right].timestamp < end:
                right += 1
            windows.append(_window(streamer_id, start, end, stream_messages[left:right]))
            start = round(start + config.stride_seconds, 9)
        result[streamer_id] = windows
    return result


def _median(history: list[SignalWindow], name: str) -> float:
    return float(statistics.median(getattr(window, name) for window in history))


def score_window(window: SignalWindow, history: list[SignalWindow], config: DetectionConfig) -> tuple[float, dict]:
    """Return an explainable 0–10 score and its raw/baseline/component evidence."""
    counts = [item.message_count for item in history]
    baseline_count = float(statistics.median(counts))
    mean_count = statistics.fmean(counts)
    baseline_unique = _median(history, "unique_participants")
    baseline_emotes = _median(history, "emote_ratio")
    baseline_caps = _median(history, "caps_ratio")

    excess = max(0.0, window.message_count - mean_count)
    frequency = _clamp(excess / ((math.sqrt(mean_count) + 1.0) * config.surprise_scale))
    relative_ratio = (window.message_count + 0.5) / (baseline_count + 0.5)
    relative_activity = _clamp(math.log2(max(1.0, relative_ratio)) / math.log2(config.relative_full_scale))

    participant_ratio = window.unique_participants / window.message_count if window.message_count else 0.0
    participant_lift_ratio = (window.unique_participants + 0.5) / (baseline_unique + 0.5)
    participant_lift = _clamp(
        math.log2(max(1.0, participant_lift_ratio)) / math.log2(config.relative_full_scale)
    )
    participants = _clamp(0.7 * participant_lift + 0.3 * participant_ratio)
    emotes = _clamp((window.emote_ratio - baseline_emotes) / max(0.25, 1.0 - baseline_emotes))
    caps = _clamp((window.caps_ratio - baseline_caps) / max(0.25, 1.0 - baseline_caps))

    components = {
        "frequency": frequency,
        "relative_activity": relative_activity,
        "participants": participants,
        "emotes": emotes,
        "caps": caps,
    }
    weights = config.weights.normalized()
    score = round(10.0 * sum(components[name] * weights[name] for name in components), 3)
    signals = {
        "window": {"start": window.start, "end": window.end},
        "current": {
            "message_count": window.message_count,
            "message_rate": round(window.message_count / (window.end - window.start), 6),
            "unique_participants": window.unique_participants,
            "participant_ratio": round(participant_ratio, 6),
            "emote_ratio": round(window.emote_ratio, 6),
            "caps_ratio": round(window.caps_ratio, 6),
        },
        "baseline": {
            "windows": len(history),
            "message_count_median": round(baseline_count, 6),
            "message_count_mean": round(mean_count, 6),
            "message_rate_median": round(baseline_count / (window.end - window.start), 6),
            "unique_participants_median": round(baseline_unique, 6),
            "emote_ratio_median": round(baseline_emotes, 6),
            "caps_ratio_median": round(baseline_caps, 6),
        },
        "relative_activity_ratio": round(relative_ratio, 6),
        "components": {name: round(value, 6) for name, value in components.items()},
        "weights": weights,
    }
    return score, signals


def _candidate(window: SignalWindow, score: float, signals: dict, config: DetectionConfig) -> Candidate:
    event_timestamp = round((window.start + window.end) / 2.0, 6)
    identity = f"{DETECTOR_VERSION}|{window.streamer_id}|{event_timestamp:.6f}"
    candidate_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return Candidate(
        id=candidate_id,
        streamer_id=window.streamer_id,
        event_timestamp=event_timestamp,
        hype_score=score,
        signals=signals,
        clip_start=max(0.0, round(event_timestamp - config.clip_pre_roll_seconds, 6)),
        clip_end=round(event_timestamp + config.clip_post_roll_seconds, 6),
        detector_version=DETECTOR_VERSION,
    )


def _deduplicate(candidates: list[Candidate], cooldown: float) -> list[Candidate]:
    if not candidates:
        return []
    candidates.sort(key=lambda candidate: (candidate.streamer_id, candidate.event_timestamp, candidate.id))
    output: list[Candidate] = []
    cluster: list[Candidate] = []
    cluster_last_event = 0.0
    cluster_clip_end = 0.0

    def finish() -> None:
        if cluster:
            output.append(max(cluster, key=lambda candidate: (candidate.hype_score, -candidate.event_timestamp)))

    for candidate in candidates:
        same_streamer = bool(cluster) and candidate.streamer_id == cluster[0].streamer_id
        related = same_streamer and (
            candidate.event_timestamp - cluster_last_event <= cooldown
            or candidate.clip_start < cluster_clip_end
        )
        if cluster and not related:
            finish()
            cluster = []
        cluster.append(candidate)
        cluster_last_event = candidate.event_timestamp
        cluster_clip_end = max(cluster_clip_end if same_streamer else 0.0, candidate.clip_end)
    finish()
    return output


def detect_candidates(messages: Iterable[ChatMessage], config: DetectionConfig | None = None) -> list[Candidate]:
    """Detect and deduplicate candidates for any number of streamers."""
    config = config or DetectionConfig()
    raw: list[Candidate] = []
    for windows in build_windows(messages, config).values():
        history: deque[SignalWindow] = deque(maxlen=config.baseline_windows)
        for window in windows:
            if len(history) >= config.min_baseline_windows:
                score, signals = score_window(window, list(history), config)
                if score >= config.hype_threshold:
                    raw.append(_candidate(window, score, signals, config))
            history.append(window)
    return _deduplicate(raw, config.cooldown_seconds)
