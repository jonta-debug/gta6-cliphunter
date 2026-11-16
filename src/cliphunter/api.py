"""Read-only FastAPI surface for local candidate inspection."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from .config import database_path
from .persistence import CandidateStore, SQLiteCandidateStore


def create_app(store: CandidateStore | None = None) -> FastAPI:
    candidate_store = store or SQLiteCandidateStore(database_path())
    app = FastAPI(title="GTA6 ClipHunter", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/candidates")
    def candidates(
        streamer_id: str | None = None,
        min_score: float = Query(default=0.0, ge=0.0, le=10.0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict]:
        return [candidate.to_dict() for candidate in candidate_store.list(
            streamer_id=streamer_id, min_score=min_score, limit=limit
        )]

    @app.get("/candidates/{candidate_id}")
    def candidate(candidate_id: str) -> dict:
        found = candidate_store.get(candidate_id)
        if found is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        return found.to_dict()

    return app
