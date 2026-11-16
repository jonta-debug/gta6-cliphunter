import json
import asyncio
import signal
from types import SimpleNamespace

import pytest

from cliphunter.cli import _install_stop_signals, main
from cliphunter.twitch import ConnectionHealth


def test_replay_command_persists_candidates(chat_scenario, tmp_path, capsys, monkeypatch):
    path = chat_scenario(
        "cli.jsonl", streamer_id="cli-streamer", baseline_messages=1, spike_messages=10
    )
    database = tmp_path / "cli.db"
    monkeypatch.setenv("CLIPHUNTER_MIN_BASELINE_WINDOWS", "6")
    monkeypatch.setenv("CLIPHUNTER_BASELINE_WINDOWS", "12")

    result = main(["replay", str(path), "--database", str(database)])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["messages"] > 0
    assert output["candidates"] == 1
    assert database.exists()


def test_live_command_reports_missing_configuration_without_connecting(monkeypatch, tmp_path):
    for name in (
        "TWITCH_CLIENT_ID", "TWITCH_USER_ACCESS_TOKEN", "TWITCH_USER_ID",
        "TWITCH_CHANNEL_IDS", "TWITCH_CHANNELS",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit, match="Twitch configuration error"):
        main(["live", "--database", str(tmp_path / "live.db")])


def test_diagnostic_command_prints_redacted_live_counters(monkeypatch, tmp_path, capsys):
    client = SimpleNamespace(health=ConnectionHealth())
    detector = SimpleNamespace(detected_candidates=0)

    class FakeMonitor:
        async def run(self, stop_event):
            client.health.status = "connected"
            client.health.connected = True
            client.health.subscriptions = 2
            client.health.received_chat_messages = 19
            detector.detected_candidates = 1
            await stop_event.wait()
            client.health.status = "stopped"
            client.health.connected = False

    async def build_monitor(_database):
        return FakeMonitor(), client, detector

    monkeypatch.setattr("cliphunter.cli._build_live_monitor", build_monitor)
    result = main([
        "twitch-diagnose", "--database", str(tmp_path / "diagnostic.db"),
        "--duration", "0.01", "--interval", "0.005",
    ])
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]

    assert result == 0
    assert any(record["websocket_connected"] for record in records)
    assert records[-1]["websocket_status"] == "stopped"
    assert records[-1]["active_subscriptions"] == 2
    assert records[-1]["received_chat_messages"] == 19
    assert records[-1]["detected_highlight_candidates"] == 1
    assert "token" not in output.lower()


def test_sigint_handler_requests_graceful_pipeline_shutdown(monkeypatch):
    callbacks = {}

    class Loop:
        def add_signal_handler(self, event, callback):
            callbacks[event] = callback

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: Loop())
    stop = asyncio.Event()

    _install_stop_signals(stop)
    callbacks[signal.SIGINT]()

    assert stop.is_set()
