from pathlib import Path
import asyncio
from unittest.mock import patch

from cliphunter.config import DetectionConfig
from cliphunter.live import LiveChatMonitor, LiveDetectionBuffer, diagnostic_snapshot
from cliphunter.persistence import SQLiteCandidateStore
from cliphunter.models import Candidate, ChatMessage
from cliphunter.replay import read_jsonl
from cliphunter.twitch import TwitchConfig, TwitchEventSubClient

FIXTURES = Path(__file__).parent / "fixtures"


def test_live_buffer_reuses_offline_detection_and_persists_once(tmp_path):
    store = SQLiteCandidateStore(tmp_path / "live.db")
    buffer = LiveDetectionBuffer(store, DetectionConfig())
    for message in read_jsonl(FIXTURES / "acceptance_realistic.jsonl"):
        buffer.add(message)

    first = buffer.evaluate()
    second = buffer.evaluate()

    assert {candidate.streamer_id for candidate in first} == {"gta_small", "gta_large"}
    assert second == []
    assert len(store.list()) == 2
    assert buffer.detected_candidates == 2


def test_live_buffer_bounds_each_channel_independently(tmp_path):
    store = SQLiteCandidateStore(tmp_path / "live.db")
    buffer = LiveDetectionBuffer(store, DetectionConfig(), retention_seconds=600)
    messages = read_jsonl(FIXTURES / "multiple_streamers.jsonl")
    for message in messages:
        buffer.add(message)

    assert set(buffer.messages) == {"small", "large"}
    assert sum(len(channel) for channel in buffer.messages.values()) == len(messages)


def test_live_buffer_does_not_persist_candidate_after_media_has_expired(tmp_path):
    store = SQLiteCandidateStore(tmp_path / "live.db")
    buffer = LiveDetectionBuffer(
        store,
        DetectionConfig(),
        media_retention_seconds=120,
    )
    buffer.add(ChatMessage("channel", 1_000, "viewer", "message"))
    stale = Candidate(
        id="stale", streamer_id="channel", event_timestamp=800,
        hype_score=8, signals={}, clip_start=770, clip_end=820,
        detector_version="test",
    )
    fresh = Candidate(
        id="fresh", streamer_id="channel", event_timestamp=980,
        hype_score=8, signals={}, clip_start=950, clip_end=1_000,
        detector_version="test",
    )

    with patch("cliphunter.live.detect_candidates", return_value=[stale, fresh]):
        detected = buffer.evaluate()

    assert [candidate.id for candidate in detected] == ["fresh"]
    assert [candidate.id for candidate in store.list()] == ["fresh"]


def test_monitor_connects_listener_to_detector_and_shuts_down(tmp_path):
    messages = read_jsonl(FIXTURES / "acceptance_realistic.jsonl")
    store = SQLiteCandidateStore(tmp_path / "monitor.db")
    detector = LiveDetectionBuffer(store, DetectionConfig())

    class FakeClient:
        stopped = False

        async def run(self, callback, stop_event):
            for message in messages:
                callback(message)
            await stop_event.wait()
            self.stopped = True

    client = FakeClient()
    monitor = LiveChatMonitor(client, detector, evaluation_interval_seconds=0.01)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        async with asyncio.timeout(1):
            while len(store.list()) < 2:
                await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert client.stopped
    assert {candidate.streamer_id for candidate in store.list()} == {"gta_small", "gta_large"}


def test_diagnostic_snapshot_is_useful_and_contains_no_credentials(tmp_path):
    detector = LiveDetectionBuffer(SQLiteCandidateStore(tmp_path / "diagnostic.db"))
    client = TwitchEventSubClient(TwitchConfig(
        client_id="client-id",
        user_access_token="secret-user-token",
        user_id="9001",
        channel_ids=("1001", "1002"),
    ))
    client.health.status = "connected"
    client.health.connected = True
    client.health.subscriptions = 2
    client.health.received_chat_messages = 17
    detector.detected_candidates = 3

    snapshot = diagnostic_snapshot(client, detector)
    rendered = __import__("json").dumps(snapshot)

    assert snapshot == {
        "websocket_status": "connected",
        "websocket_connected": True,
        "active_subscriptions": 2,
        "received_chat_messages": 17,
        "detected_highlight_candidates": 3,
        "shadow_reaction_candidates": 0,
        "shadow_reaction_failures": 0,
        "reconnects": 0,
        "connection_failures": 0,
        "invalid_messages": 0,
    }
    assert "secret-user-token" not in rendered
    assert "client-id" not in rendered
