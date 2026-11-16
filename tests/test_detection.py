from pathlib import Path

import pytest

from cliphunter.config import DetectionConfig
from cliphunter.detection import detect_candidates
from cliphunter.replay import read_jsonl

FIXTURES = Path(__file__).parent / "fixtures"


def config(**changes) -> DetectionConfig:
    values = {
        "window_seconds": 10,
        "stride_seconds": 5,
        "baseline_windows": 12,
        "min_baseline_windows": 6,
        "hype_threshold": 5.5,
        "cooldown_seconds": 30,
        "clip_pre_roll_seconds": 20,
        "clip_post_roll_seconds": 10,
    }
    values.update(changes)
    return DetectionConfig(**values)


def test_normal_activity_does_not_generate_candidate(chat_scenario):
    path = chat_scenario("normal.jsonl", streamer_id="normal", baseline_messages=3)

    assert detect_candidates(read_jsonl(path), config()) == []


def test_small_streamer_spike_is_detected_and_explained(chat_scenario):
    path = chat_scenario(
        "small-spike.jsonl", streamer_id="small", baseline_messages=1, spike_messages=8
    )

    candidates = detect_candidates(read_jsonl(path), config())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.streamer_id == "small"
    assert 5.5 <= candidate.hype_score <= 10
    assert candidate.clip_start == candidate.event_timestamp - 20
    assert candidate.clip_end == candidate.event_timestamp + 10
    assert set(candidate.signals["components"]) == {
        "frequency", "relative_activity", "participants", "emotes", "caps"
    }
    assert candidate.signals["current"]["message_count"] > candidate.signals["baseline"]["message_count_median"]
    assert candidate.signals["components"]["emotes"] > 0
    assert candidate.signals["components"]["caps"] > 0
    assert abs(sum(candidate.signals["weights"].values()) - 1.0) < 1e-9


def test_large_streamer_spike_uses_its_own_baseline(chat_scenario):
    path = chat_scenario(
        "large-spike.jsonl", streamer_id="large", baseline_messages=12, spike_messages=48
    )

    candidates = detect_candidates(read_jsonl(path), config())

    assert len(candidates) == 1
    assert candidates[0].signals["baseline"]["message_count_median"] >= 20
    assert candidates[0].hype_score >= 5.5


def test_overlapping_windows_and_repeated_burst_are_deduplicated(chat_scenario):
    path = chat_scenario(
        "duplicate.jsonl",
        streamer_id="duplicate",
        baseline_messages=2,
        spike_messages=12,
        duplicate_spike=True,
    )

    candidates = detect_candidates(read_jsonl(path), config(cooldown_seconds=45))

    assert len(candidates) == 1


def test_replay_is_deterministic(chat_scenario):
    path = chat_scenario(
        "deterministic.jsonl", streamer_id="stable", baseline_messages=2, spike_messages=15
    )
    messages = read_jsonl(path)

    first = detect_candidates(messages, config())
    second = detect_candidates(reversed(messages), config())

    assert [candidate.to_dict() for candidate in first] == [candidate.to_dict() for candidate in second]


def test_multiple_streamers_keep_independent_baselines(chat_scenario):
    small = chat_scenario(
        "small.jsonl", streamer_id="small", baseline_messages=1, spike_messages=8
    )
    large = chat_scenario(
        "large.jsonl", streamer_id="large", baseline_messages=10, spike_messages=40
    )

    candidates = detect_candidates(read_jsonl([small, large]), config())

    assert {candidate.streamer_id for candidate in candidates} == {"small", "large"}


def test_realistic_acceptance_replay_has_only_planted_events():
    messages = read_jsonl(FIXTURES / "acceptance_realistic.jsonl")

    candidates = detect_candidates(messages, DetectionConfig())
    by_streamer = {candidate.streamer_id: candidate for candidate in candidates}

    assert len(messages) == 574
    assert set(by_streamer) == {"gta_small", "gta_large"}
    assert by_streamer["gta_small"].hype_score == pytest.approx(9.0)
    assert by_streamer["gta_large"].hype_score == pytest.approx(8.559)
    assert by_streamer["gta_small"].signals["relative_activity_ratio"] > 10
    assert by_streamer["gta_large"].signals["relative_activity_ratio"] > 3
