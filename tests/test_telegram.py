from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from cliphunter.media_worker import MediaWorker
from cliphunter.models import Candidate
from cliphunter.persistence import SQLiteCandidateStore
from cliphunter.reviews import SQLiteReviewStore
from cliphunter.telegram import (
    ReviewCoordinator,
    TelegramAPIError,
    TelegramBotAPI,
    TelegramCallbackWorker,
    TelegramConfig,
    TelegramConfigurationError,
)


def candidate(identifier: str = "candidate-1") -> Candidate:
    return Candidate(
        id=identifier,
        streamer_id="430023505",
        event_timestamp=1_794_830_400.0,
        hype_score=8.75,
        signals={},
        clip_start=1_794_830_370.0,
        clip_end=1_794_830_420.0,
        detector_version="test",
    )


class FakeTelegramRequest:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def __call__(self, method, fields, file_path):
        self.calls.append((method, fields, file_path))
        if self.fail:
            raise TelegramAPIError("mock Telegram failure")
        if method == "sendVideo":
            return {"ok": True, "result": {"message_id": 77, "chat": {"id": -1001}}}
        return {"ok": True, "result": True}


def config(*, attempts: int = 1) -> TelegramConfig:
    return TelegramConfig(
        bot_token="secret-bot-token",
        chat_id="-1001",
        allowed_user_ids=frozenset({42}),
        max_attempts=attempts,
    )


def test_telegram_is_disabled_without_configuration_and_rejects_partial_config(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_IDS"):
        monkeypatch.delenv(name, raising=False)
    assert TelegramConfig.from_env_optional() is None

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-bot-token")
    with pytest.raises(TelegramConfigurationError, match="set TELEGRAM"):
        TelegramConfig.from_env_optional()
    assert "secret-bot-token" not in repr(config())


def test_callback_poller_lease_prevents_multiple_workers(tmp_path):
    store = SQLiteReviewStore(tmp_path / "reviews.db")

    assert store.acquire_callback_lease("first", ttl_seconds=60)
    assert not store.acquire_callback_lease("second", ttl_seconds=60)
    store.release_callback_lease("first")
    assert store.acquire_callback_lease("second", ttl_seconds=60)


class FakeBuffer:
    last_error = ""

    async def extract_clip(self, _id, _start, _end, output, **_kwargs):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"original")

    async def validate_clip(self, _path):
        return None


class FakeRenderer:
    async def render(self, _input, output, **_kwargs):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"vertical")

    async def validate(self, _input, _output, **_kwargs):
        return None


def build_worker(tmp_path, request):
    review_store = SQLiteReviewStore(tmp_path / "reviews.db")
    telegram_config = config()
    coordinator = ReviewCoordinator(
        review_store, TelegramBotAPI(telegram_config, request=request)
    )
    worker = MediaWorker(
        FakeBuffer(),
        SQLiteCandidateStore(tmp_path / "reviews.db"),
        "430023505",
        tmp_path / "clips",
        renderer=FakeRenderer(),
        rendered_directory=tmp_path / "rendered",
        review_coordinator=coordinator,
        streamer_label="xrohat",
    )
    return worker, review_store, coordinator


def test_completed_render_creates_one_pending_notification_across_restart(tmp_path):
    request = FakeTelegramRequest()
    worker, store, coordinator = build_worker(tmp_path, request)
    item = candidate()

    asyncio.run(worker._extract(item))
    asyncio.run(coordinator.complete(
        item,
        streamer_label="xrohat",
        original_path=tmp_path / "clips" / f"{item.id}.mp4",
        rendered_path=tmp_path / "rendered" / "clean_gaming" / f"{item.id}.mp4",
    ))

    review = store.get(item.id)
    sends = [call for call in request.calls if call[0] == "sendVideo"]
    assert review is not None
    assert review.status == "pending"
    assert review.notification_state == "sent"
    assert review.telegram_message_id == 77
    assert len(sends) == 1
    assert sends[0][2].read_bytes() == b"vertical"
    assert "xrohat" in sends[0][1]["caption"]
    assert "8.75/10" in sends[0][1]["caption"]
    markup = json.loads(sends[0][1]["reply_markup"])
    assert [button["text"] for button in markup["inline_keyboard"][0]] == ["Approve", "Reject"]


def callback_update(review, *, action="a", user_id=42, query_id="query-1"):
    return {
        "update_id": 1,
        "callback_query": {
            "id": query_id,
            "from": {"id": user_id},
            "data": f"review:{action}:{review.candidate_id}:{review.callback_token}",
            "message": {
                "message_id": 77,
                "chat": {"id": -1001},
                "caption": "GTA6 ClipHunter review",
            },
        },
    }


def test_authorized_approve_reject_and_duplicate_callbacks_are_idempotent(tmp_path):
    store = SQLiteReviewStore(tmp_path / "reviews.db")
    request = FakeTelegramRequest()
    telegram_config = config()
    worker = TelegramCallbackWorker(
        telegram_config, TelegramBotAPI(telegram_config, request=request), store
    )
    approved = store.create_pending(
        candidate("approve-me"), streamer_label="xrohat", clip_duration=50,
        original_path="original.mp4", rendered_path="vertical.mp4",
    )
    rejected = store.create_pending(
        candidate("reject-me"), streamer_label="xrohat", clip_duration=50,
        original_path="original-2.mp4", rendered_path="vertical-2.mp4",
    )
    unauthorized = store.create_pending(
        candidate("protected"), streamer_label="xrohat", clip_duration=50,
        original_path="original-3.mp4", rendered_path="vertical-3.mp4",
    )

    assert asyncio.run(worker.handle_update(callback_update(approved))) == "updated"
    decided_at = store.get(approved.candidate_id).decided_at
    assert asyncio.run(worker.handle_update(callback_update(
        approved, query_id="query-duplicate"
    ))) == "duplicate"
    assert asyncio.run(worker.handle_update(callback_update(
        approved, action="r", query_id="query-conflict"
    ))) == "conflict"
    assert asyncio.run(worker.handle_update(callback_update(
        rejected, action="r", query_id="query-reject-menu"
    ))) == "reason_required"
    assert store.get(rejected.candidate_id).status == "pending"
    reason_markup = json.loads([
        call for call in request.calls if call[0] == "editMessageReplyMarkup"
    ][0][1]["reply_markup"])
    assert {
        button[0]["callback_data"].split(":", 3)[1]
        for button in reason_markup["inline_keyboard"]
    } == {"advertisement", "boring", "wrong_timing", "duplicate"}
    assert asyncio.run(worker.handle_update(callback_update(
        rejected, action="wrong_timing", query_id="query-reject"
    ))) == "updated"
    assert asyncio.run(worker.handle_update(callback_update(
        rejected, action="wrong_timing", query_id="query-reject-duplicate"
    ))) == "duplicate"
    assert asyncio.run(worker.handle_update(callback_update(
        rejected, action="boring", query_id="query-reject-conflict"
    ))) == "conflict"
    assert asyncio.run(worker.handle_update(callback_update(
        unauthorized, action="boring", user_id=99, query_id="query-unauthorized"
    ))) == "unauthorized"

    assert store.get(approved.candidate_id).status == "approved"
    assert store.get(approved.candidate_id).decided_at == decided_at
    assert store.get(rejected.candidate_id).status == "rejected"
    assert store.get(rejected.candidate_id).rejection_reason == "wrong_timing"
    assert store.get(unauthorized.candidate_id).status == "pending"
    assert len([call for call in request.calls if call[0] == "editMessageReplyMarkup"]) == 1
    assert len([call for call in request.calls if call[0] == "editMessageCaption"]) == 2


@pytest.mark.parametrize(
    "reason", ["advertisement", "boring", "wrong_timing", "duplicate"]
)
def test_each_structured_rejection_reason_is_persisted(tmp_path, reason):
    store = SQLiteReviewStore(tmp_path / f"{reason}.db")
    request = FakeTelegramRequest()
    telegram_config = config()
    worker = TelegramCallbackWorker(
        telegram_config, TelegramBotAPI(telegram_config, request=request), store
    )
    review = store.create_pending(
        candidate(f"reject-{reason}"), streamer_label="xrohat", clip_duration=50,
        original_path="original.mp4", rendered_path="vertical.mp4",
    )

    assert asyncio.run(worker.handle_update(callback_update(
        review, action=reason, query_id=f"query-{reason}"
    ))) == "updated"

    decided = store.get(review.candidate_id)
    assert decided.status == "rejected"
    assert decided.rejection_reason == reason


def test_existing_review_database_is_migrated_without_losing_records(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE reviews (
                candidate_id TEXT PRIMARY KEY, streamer_id TEXT NOT NULL,
                streamer_label TEXT NOT NULL, hype_score REAL NOT NULL,
                event_timestamp REAL NOT NULL, clip_duration REAL NOT NULL,
                original_path TEXT NOT NULL, rendered_path TEXT NOT NULL,
                status TEXT NOT NULL, callback_token TEXT NOT NULL UNIQUE,
                notification_state TEXT NOT NULL, notification_attempts INTEGER NOT NULL DEFAULT 0,
                telegram_chat_id TEXT, telegram_message_id INTEGER, decided_by INTEGER,
                decided_at TEXT, last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("""
            INSERT INTO reviews (
                candidate_id, streamer_id, streamer_label, hype_score,
                event_timestamp, clip_duration, original_path, rendered_path,
                status, callback_token, notification_state
            ) VALUES ('legacy', '1', 'channel', 7, 100, 30,
                      'original.mp4', 'rendered.mp4', 'rejected', 'token', 'sent')
        """)

    review = SQLiteReviewStore(database).get("legacy")

    assert review is not None
    assert review.status == "rejected"
    assert review.rejection_reason is None


def test_telegram_failure_does_not_remove_video_or_raise_from_media_worker(tmp_path):
    request = FakeTelegramRequest(fail=True)
    worker, store, _ = build_worker(tmp_path, request)
    item = candidate("delivery-failure")

    asyncio.run(worker._extract(item))

    original = tmp_path / "clips" / f"{item.id}.mp4"
    rendered = tmp_path / "rendered" / "clean_gaming" / f"{item.id}.mp4"
    review = store.get(item.id)
    assert original.read_bytes() == b"original"
    assert rendered.read_bytes() == b"vertical"
    assert review is not None and review.notification_state == "failed"
    assert item.id in worker.review_errors
    assert item.id not in worker.errors
    assert item.id not in worker.render_errors
