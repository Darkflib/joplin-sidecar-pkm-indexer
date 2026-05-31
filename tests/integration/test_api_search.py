"""Tests for /api/search (PRD §13.2, §18.4)."""

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar import db
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig
from pkm_sidecar.repositories import NoteRepository

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def client(cfg: AppConfig):
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    repo = NoteRepository(conn)
    with repo.transaction():
        repo.upsert_note(
            {"id": "n1", "title": "RabbitMQ", "body": "configure RabbitMQ clustering"}, indexed_at=1
        )
        repo.upsert_note({"id": "n2", "title": "Other", "body": "kafka streams"}, indexed_at=1)
    conn.close()
    with TestClient(create_app(cfg, start_indexer_loop=False)) as c:
        yield c


def test_search_returns_hit_with_snippet(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "RabbitMQ"}, headers=AUTH)
    assert resp.status_code == 200
    hits = resp.json()
    assert len(hits) == 1
    assert hits[0]["id"] == "n1"
    assert hits[0]["snippet"]  # PRD §18.4: snippet/preview present


def test_search_blank_query_422(client: TestClient) -> None:
    assert client.get("/api/search", params={"q": "   "}, headers=AUTH).status_code == 422
    assert client.get("/api/search", params={"q": ""}, headers=AUTH).status_code == 422


def test_search_no_match(client: TestClient) -> None:
    assert client.get("/api/search", params={"q": "nonexistentterm"}, headers=AUTH).json() == []
