"""SQLite persistence for manual clip review and Telegram delivery state."""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .models import Candidate


@dataclass(frozen=True)
class Review:
    candidate_id: str
    streamer_id: str
    streamer_label: str
    hype_score: float
    event_timestamp: float
    clip_duration: float
    original_path: str
    rendered_path: str
    status: str
    callback_token: str
    notification_state: str
    telegram_chat_id: str | None
    telegram_message_id: int | None
    decided_by: int | None
    decided_at: str | None
    rejection_reason: str | None
    last_error: str


REJECTION_REASONS = frozenset({
    "advertisement", "boring", "wrong_timing", "duplicate",
})


class SQLiteReviewStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    candidate_id TEXT PRIMARY KEY,
                    streamer_id TEXT NOT NULL,
                    streamer_label TEXT NOT NULL,
                    hype_score REAL NOT NULL,
                    event_timestamp REAL NOT NULL,
                    clip_duration REAL NOT NULL CHECK (clip_duration > 0),
                    original_path TEXT NOT NULL,
                    rendered_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
                    callback_token TEXT NOT NULL UNIQUE,
                    notification_state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (notification_state IN ('pending', 'sending', 'sent', 'failed')),
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    telegram_chat_id TEXT,
                    telegram_message_id INTEGER,
                    decided_by INTEGER,
                    decided_at TEXT,
                    rejection_reason TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(reviews)")
            }
            if "rejection_reason" not in columns:
                connection.execute("ALTER TABLE reviews ADD COLUMN rejection_reason TEXT")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS review_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

    @staticmethod
    def _review(row: sqlite3.Row) -> Review:
        return Review(
            candidate_id=row["candidate_id"],
            streamer_id=row["streamer_id"],
            streamer_label=row["streamer_label"],
            hype_score=row["hype_score"],
            event_timestamp=row["event_timestamp"],
            clip_duration=row["clip_duration"],
            original_path=row["original_path"],
            rendered_path=row["rendered_path"],
            status=row["status"],
            callback_token=row["callback_token"],
            notification_state=row["notification_state"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            rejection_reason=row["rejection_reason"],
            last_error=row["last_error"],
        )

    def create_pending(
        self,
        candidate: Candidate,
        *,
        streamer_label: str,
        clip_duration: float,
        original_path: str | Path,
        rendered_path: str | Path,
    ) -> Review:
        if clip_duration <= 0:
            raise ValueError("clip duration must be positive")
        with self._connect() as connection:
            connection.execute("""
                INSERT OR IGNORE INTO reviews (
                    candidate_id, streamer_id, streamer_label, hype_score,
                    event_timestamp, clip_duration, original_path, rendered_path,
                    callback_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate.id,
                candidate.streamer_id,
                streamer_label,
                candidate.hype_score,
                candidate.event_timestamp,
                clip_duration,
                str(original_path),
                str(rendered_path),
                secrets.token_urlsafe(9),
            ))
        review = self.get(candidate.id)
        if review is None:
            raise RuntimeError("failed to create review record")
        return review

    def get(self, candidate_id: str) -> Review | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return self._review(row) if row else None

    def claim_notification(self, candidate_id: str) -> Review | None:
        with self._connect() as connection:
            cursor = connection.execute("""
                UPDATE reviews
                SET notification_state = 'sending',
                    notification_attempts = notification_attempts + 1,
                    last_error = '', updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ?
                  AND notification_state IN ('pending', 'failed')
                  AND telegram_message_id IS NULL
            """, (candidate_id,))
        return self.get(candidate_id) if cursor.rowcount == 1 else None

    def mark_notified(self, candidate_id: str, chat_id: str, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute("""
                UPDATE reviews
                SET notification_state = 'sent', telegram_chat_id = ?,
                    telegram_message_id = ?, last_error = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ? AND notification_state = 'sending'
            """, (chat_id, message_id, candidate_id))

    def mark_notification_failed(self, candidate_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute("""
                UPDATE reviews
                SET notification_state = 'failed', last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ? AND notification_state = 'sending'
            """, (error[:500], candidate_id))

    def decide(
        self,
        candidate_id: str,
        callback_token: str,
        decision: str,
        user_id: int,
        rejection_reason: str | None = None,
    ) -> str:
        if decision not in {"approved", "rejected"}:
            return "invalid"
        if decision == "approved" and rejection_reason is not None:
            return "invalid"
        if decision == "rejected" and rejection_reason not in REJECTION_REASONS:
            return "invalid"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, callback_token, rejection_reason FROM reviews WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None or not secrets.compare_digest(row["callback_token"], callback_token):
                return "invalid"
            if row["status"] == decision:
                if decision == "approved" or row["rejection_reason"] == rejection_reason:
                    return "duplicate"
                return "conflict"
            if row["status"] != "pending":
                return "conflict"
            cursor = connection.execute("""
                UPDATE reviews
                SET status = ?, rejection_reason = ?, decided_by = ?, decided_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ? AND status = 'pending'
            """, (decision, rejection_reason, user_id, candidate_id))
        return "updated" if cursor.rowcount == 1 else "conflict"

    def callback_status(self, candidate_id: str, callback_token: str) -> str | None:
        review = self.get(candidate_id)
        if review is None or not secrets.compare_digest(review.callback_token, callback_token):
            return None
        return review.status

    def update_offset(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM review_state WHERE key = 'telegram_update_offset'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def set_update_offset(self, offset: int) -> None:
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO review_state (key, value) VALUES ('telegram_update_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (str(offset),))

    def acquire_callback_lease(self, owner: str, *, ttl_seconds: float = 90.0) -> bool:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM review_state WHERE key = 'telegram_callback_lease'"
            ).fetchone()
            if row:
                current_owner, expires_value = row["value"].split(":", 1)
                if current_owner != owner and float(expires_value) > now:
                    return False
            connection.execute("""
                INSERT INTO review_state (key, value)
                VALUES ('telegram_callback_lease', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (f"{owner}:{now + ttl_seconds}",))
        return True

    def refresh_callback_lease(self, owner: str, *, ttl_seconds: float = 90.0) -> bool:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM review_state WHERE key = 'telegram_callback_lease'"
            ).fetchone()
            if not row or row["value"].split(":", 1)[0] != owner:
                return False
            connection.execute(
                "UPDATE review_state SET value = ? WHERE key = 'telegram_callback_lease'",
                (f"{owner}:{now + ttl_seconds}",),
            )
        return True

    def release_callback_lease(self, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM review_state WHERE key = 'telegram_callback_lease' AND value LIKE ?",
                (f"{owner}:%",),
            )
