from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def chat_scenario(tmp_path: Path):
    """Write deterministic normal/spike JSONL scenarios for any channel size."""

    def write(
        name: str,
        *,
        streamer_id: str,
        baseline_messages: int,
        spike_messages: int = 0,
        duplicate_spike: bool = False,
    ) -> Path:
        records = []
        # One bucket every five seconds. A ten-second replay window normally
        # observes two buckets, producing a stable adaptive baseline.
        for bucket in range(16):
            timestamp = bucket * 5.0 + 1.0
            for index in range(baseline_messages):
                records.append({
                    "streamer_id": streamer_id,
                    "timestamp": timestamp + index / max(100, baseline_messages * 10),
                    "username": f"regular-{index % max(1, baseline_messages)}",
                    "text": "normal conversation",
                    "emotes": [],
                })

        if spike_messages:
            for offset, timestamp in enumerate((81.0, 86.0) if duplicate_spike else (81.0,)):
                for index in range(spike_messages):
                    records.append({
                        "streamer_id": streamer_id,
                        "timestamp": timestamp + index / max(100, spike_messages * 10),
                        "username": f"hype-{offset}-{index}",
                        "text": "NO WAY THAT WAS INSANE" if index % 2 == 0 else "what a play",
                        "emotes": ["hype"] if index % 3 == 0 else [],
                    })

        path = tmp_path / name
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    return write
