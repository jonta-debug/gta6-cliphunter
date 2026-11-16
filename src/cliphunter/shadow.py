"""Persistence for shadow-only detector output."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .reaction import ShadowCandidate


class SQLiteShadowCandidateStore:
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
                CREATE TABLE IF NOT EXISTS shadow_candidates (
                    id TEXT PRIMARY KEY,
                    streamer_id TEXT NOT NULL,
                    event_timestamp REAL NOT NULL,
                    score REAL NOT NULL CHECK (score >= 0 AND score <= 10),
                    window_start REAL NOT NULL,
                    window_end REAL NOT NULL,
                    categories_json TEXT NOT NULL,
                    detector_version TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS shadow_event_idx "
                "ON shadow_candidates (event_timestamp DESC, id)"
            )

    def save_many(self, candidates: list[ShadowCandidate]) -> None:
        if not candidates:
            return
        with self._connect() as connection:
            connection.executemany("""
                INSERT INTO shadow_candidates (
                    id, streamer_id, event_timestamp, score, window_start,
                    window_end, categories_json, detector_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    event_timestamp = excluded.event_timestamp,
                    score = excluded.score,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    categories_json = excluded.categories_json,
                    detector_version = excluded.detector_version,
                    updated_at = CURRENT_TIMESTAMP
            """, [(
                item.id,
                item.streamer_id,
                item.event_timestamp,
                item.score,
                item.window_start,
                item.window_end,
                json.dumps(item.categories, sort_keys=True, separators=(",", ":")),
                item.detector_version,
            ) for item in candidates])

    def list(self, *, streamer_id: str | None = None, limit: int = 100) -> list[ShadowCandidate]:
        conditions: list[str] = []
        parameters: list[object] = []
        if streamer_id is not None:
            conditions.append("streamer_id = ?")
            parameters.append(streamer_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_candidates" + where
                + " ORDER BY event_timestamp DESC, id LIMIT ?",
                parameters,
            ).fetchall()
        return [ShadowCandidate(
            id=row["id"],
            streamer_id=row["streamer_id"],
            event_timestamp=row["event_timestamp"],
            score=row["score"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            categories=json.loads(row["categories_json"]),
            detector_version=row["detector_version"],
        ) for row in rows]
