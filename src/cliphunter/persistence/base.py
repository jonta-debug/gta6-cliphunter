"""Persistence contract kept independent of a specific database engine."""

from __future__ import annotations

from typing import Protocol

from ..models import Candidate


class CandidateStore(Protocol):
    def save(self, candidate: Candidate) -> None: ...

    def save_many(self, candidates: list[Candidate]) -> None: ...

    def get(self, candidate_id: str) -> Candidate | None: ...

    def list(self, *, streamer_id: str | None = None, min_score: float = 0.0, limit: int = 100) -> list[Candidate]: ...
