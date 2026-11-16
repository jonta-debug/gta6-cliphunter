"""SQLite candidate store for local development."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..models import Candidate


class SQLiteCandidateStore:
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
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    streamer_id TEXT NOT NULL,
                    event_timestamp REAL NOT NULL,
                    hype_score REAL NOT NULL CHECK (hype_score >= 0 AND hype_score <= 10),
                    signals_json TEXT NOT NULL,
                    clip_start REAL NOT NULL,
                    clip_end REAL NOT NULL,
                    detector_version TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS candidates_event_idx "
                "ON candidates (event_timestamp DESC, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS candidates_streamer_idx "
                "ON candidates (streamer_id, event_timestamp DESC)"
            )

    @staticmethod
    def _values(candidate: Candidate) -> tuple:
        return (
            candidate.id,
            candidate.streamer_id,
            candidate.event_timestamp,
            candidate.hype_score,
            json.dumps(candidate.signals, sort_keys=True, separators=(",", ":")),
            candidate.clip_start,
            candidate.clip_end,
            candidate.detector_version,
        )

    def save(self, candidate: Candidate) -> None:
        self.save_many([candidate])

    def save_many(self, candidates: list[Candidate]) -> None:
        if not candidates:
            return
        with self._connect() as connection:
            connection.executemany("""
                INSERT INTO candidates (
                    id, streamer_id, event_timestamp, hype_score, signals_json,
                    clip_start, clip_end, detector_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    streamer_id=excluded.streamer_id,
                    event_timestamp=excluded.event_timestamp,
                    hype_score=excluded.hype_score,
                    signals_json=excluded.signals_json,
                    clip_start=excluded.clip_start,
                    clip_end=excluded.clip_end,
                    detector_version=excluded.detector_version
            """, [self._values(candidate) for candidate in candidates])

    @staticmethod
    def _candidate(row: sqlite3.Row) -> Candidate:
        return Candidate(
            id=row["id"],
            streamer_id=row["streamer_id"],
            event_timestamp=row["event_timestamp"],
            hype_score=row["hype_score"],
            signals=json.loads(row["signals_json"]),
            clip_start=row["clip_start"],
            clip_end=row["clip_end"],
            detector_version=row["detector_version"],
        )

    def get(self, candidate_id: str) -> Candidate | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        return self._candidate(row) if row else None

    def list(self, *, streamer_id: str | None = None, min_score: float = 0.0, limit: int = 100) -> list[Candidate]:
        conditions = ["hype_score >= ?"]
        parameters: list[object] = [min_score]
        if streamer_id is not None:
            conditions.append("streamer_id = ?")
            parameters.append(streamer_id)
        parameters.append(limit)
        query = (
            "SELECT * FROM candidates WHERE " + " AND ".join(conditions)
            + " ORDER BY event_timestamp DESC, id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._candidate(row) for row in rows]
