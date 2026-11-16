from fastapi.testclient import TestClient

from cliphunter.api import create_app
from cliphunter.models import Candidate
from cliphunter.persistence import SQLiteCandidateStore


def candidate(identifier="candidate-1", streamer="streamer-a", score=7.25):
    return Candidate(
        id=identifier,
        streamer_id=streamer,
        event_timestamp=100.0,
        hype_score=score,
        signals={"components": {"frequency": 1.0}},
        clip_start=70.0,
        clip_end=120.0,
        detector_version="test-v1",
    )


def test_sqlite_store_is_idempotent_and_filterable(tmp_path):
    store = SQLiteCandidateStore(tmp_path / "candidates.db")
    original = candidate()
    updated = candidate(score=8.5)

    store.save(original)
    store.save(updated)
    store.save(candidate("candidate-2", "streamer-b", 6.0))

    assert store.get(original.id).hype_score == 8.5
    assert [item.id for item in store.list(streamer_id="streamer-a")] == ["candidate-1"]
    assert [item.id for item in store.list(min_score=8.0)] == ["candidate-1"]


def test_api_health_list_detail_and_not_found(tmp_path):
    store = SQLiteCandidateStore(tmp_path / "api.db")
    store.save(candidate())
    client = TestClient(create_app(store))

    assert client.get("/health").json() == {"status": "ok"}
    listing = client.get("/candidates", params={"min_score": 7}).json()
    assert [item["id"] for item in listing] == ["candidate-1"]
    assert client.get("/candidates/candidate-1").json()["signals"]["components"]["frequency"] == 1.0
    assert client.get("/candidates/missing").status_code == 404


def test_api_validates_query_parameters(tmp_path):
    client = TestClient(create_app(SQLiteCandidateStore(tmp_path / "api.db")))

    assert client.get("/candidates", params={"min_score": 11}).status_code == 422
    assert client.get("/candidates", params={"limit": 0}).status_code == 422
