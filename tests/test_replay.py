from pathlib import Path

import pytest

from cliphunter.replay import ReplayFormatError, read_jsonl

FIXTURES = Path(__file__).parent / "fixtures"


def test_reads_multiple_streamers_and_timestamp_formats():
    messages = read_jsonl(FIXTURES / "multiple_streamers.jsonl")

    assert [message.streamer_id for message in messages] == ["small", "large", "small", "large"]
    assert messages[2].timestamp == 5.0
    assert messages[2].emote_count == 1
    assert messages[3].emote_count == 1


def test_empty_chat_log_returns_no_messages():
    assert read_jsonl(FIXTURES / "empty.jsonl") == []


def test_malformed_record_reports_file_and_line():
    with pytest.raises(ReplayFormatError) as raised:
        read_jsonl(FIXTURES / "malformed.jsonl")

    assert raised.value.line_number == 1
    assert "timestamp" in raised.value.reason
    assert "malformed.jsonl:1" in str(raised.value)


def test_multiple_files_have_stable_order(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text('{"streamer_id":"a","timestamp":2,"username":"u","text":"later"}\n')
    second.write_text('{"streamer_id":"b","timestamp":1,"username":"u","text":"earlier"}\n')

    messages = read_jsonl([first, second])

    assert [(message.streamer_id, message.timestamp) for message in messages] == [("b", 1.0), ("a", 2.0)]


def test_timezone_less_iso_timestamp_is_deterministically_utc(tmp_path):
    path = tmp_path / "iso.jsonl"
    path.write_text(
        '{"streamer_id":"a","timestamp":"1970-01-01T00:00:05",'
        '"username":"u","text":"hello"}\n'
    )

    assert read_jsonl(path)[0].timestamp == 5.0
