from __future__ import annotations

from cliphunter.config import DetectionConfig
from cliphunter.live import LiveDetectionBuffer
from cliphunter.models import ChatEmote, ChatMessage
from cliphunter.persistence import SQLiteCandidateStore
from cliphunter.reaction import ReactionDetector, ReactionDetectorConfig, ReactionMatcher
from cliphunter.shadow import SQLiteShadowCandidateStore


def message(timestamp, user, text, *, emotes=()):
    return ChatMessage(
        streamer_id="channel-1",
        timestamp=float(timestamp),
        username=user,
        user_id=user,
        text=text,
        emotes=emotes,
        emote_count=len(emotes),
    )


def reaction_config(**changes):
    values = {
        "window_seconds": 5,
        "stride_seconds": 5,
        "baseline_windows": 8,
        "min_baseline_windows": 4,
        "cooldown_seconds": 15,
        "minimum_distinct_users": 2,
        "minimum_effective_reactions": 2,
        "minimum_increase": 1,
        "relative_lift_threshold": 2,
        "full_lift": 6,
        "repeat_user_weight": 0.2,
        "score_threshold": 3,
    }
    values.update(changes)
    return ReactionDetectorConfig(**values)


def baseline_messages():
    return [message(start + 1, f"normal-{start}", "ordinary chat") for start in range(0, 25, 5)]


def test_laughter_matching_is_case_insensitive_and_normalizes_repeated_ha():
    matcher = ReactionMatcher()

    assert matcher.match(message(1, "one", "HAHAHA")) == frozenset({"laughter"})
    assert matcher.match(message(1, "one", "hahahahaha")) == frozenset({"laughter"})
    assert matcher.match(message(1, "one", "LoL")) == frozenset({"laughter"})
    assert matcher.match(message(1, "one", "😂🤣")) == frozenset({"laughter"})


def test_reaction_matching_avoids_unrelated_substrings_and_uses_emote_names():
    matcher = ReactionMatcher()

    assert matcher.match(message(1, "one", "knowledge wholesome lmaoing")) == frozenset()
    assert matcher.match(message(1, "one", "lowercase winner")) == frozenset()
    assert matcher.match(message(
        1, "one", "emote", emotes=(ChatEmote(id="1", text="KEKW"),)
    )) == frozenset({"laughter"})


def test_distinct_users_create_grouped_multi_category_reaction_wave():
    messages = baseline_messages() + [
        message(26 + index * 0.2, f"reactor-{index}", "HAHAHA OMG")
        for index in range(4)
    ]

    candidates = ReactionDetector(reaction_config()).detect(messages)

    assert len(candidates) == 1
    assert set(candidates[0].categories) == {"laughter", "surprise"}
    assert candidates[0].categories["laughter"]["reaction_count"] == 4
    assert candidates[0].categories["laughter"]["distinct_users"] == 4
    assert candidates[0].categories["laughter"]["score"] >= 3


def test_repeated_messages_from_one_user_do_not_form_a_wave():
    messages = baseline_messages() + [
        message(26 + index * 0.1, "same-user", "LMAO") for index in range(12)
    ]

    assert ReactionDetector(reaction_config()).detect(messages) == []


def test_overlapping_reaction_windows_are_grouped_into_one_event():
    config = reaction_config(window_seconds=10, stride_seconds=5)
    messages = baseline_messages() + [
        message(26 + index, f"viewer-{index}", "NO WAY") for index in range(8)
    ]

    candidates = ReactionDetector(config).detect(messages)

    assert len(candidates) == 1
    assert set(candidates[0].categories) == {"surprise"}
    assert candidates[0].window_end - candidates[0].window_start > config.window_seconds


def test_shadow_detection_persists_separately_and_never_returns_clip_candidate(tmp_path):
    production = SQLiteCandidateStore(tmp_path / "detectors.db")
    shadow = SQLiteShadowCandidateStore(tmp_path / "detectors.db")
    buffer = LiveDetectionBuffer(
        production,
        DetectionConfig(hype_threshold=10),
        shadow_detector=ReactionDetector(reaction_config()),
        shadow_store=shadow,
    )
    for item in baseline_messages() + [
        message(26 + index * 0.2, f"reactor-{index}", "BRUH 💀")
        for index in range(4)
    ]:
        buffer.add(item)

    production_candidates = buffer.evaluate()

    assert production_candidates == []
    assert production.list() == []
    assert len(shadow.list()) == 1
    assert set(shadow.list()[0].categories) == {"funny_failure"}


def test_shadow_failure_does_not_interrupt_production_detection(tmp_path):
    class FailingShadowDetector:
        def detect(self, _messages):
            raise RuntimeError("expected shadow failure")

    buffer = LiveDetectionBuffer(
        SQLiteCandidateStore(tmp_path / "isolated.db"),
        DetectionConfig(),
        shadow_detector=FailingShadowDetector(),
        shadow_store=SQLiteShadowCandidateStore(tmp_path / "isolated.db"),
    )
    for item in baseline_messages():
        buffer.add(item)

    assert buffer.evaluate() == []
    assert buffer.shadow_failures == 1
