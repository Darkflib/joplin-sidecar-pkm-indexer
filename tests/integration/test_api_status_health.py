"""Tests for /api/status: auth-gating, shape, and token safety (PRD §13.2)."""

from fastapi.testclient import TestClient

from pkm_sidecar import __version__
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig

AUTH = {"Authorization": "Bearer test-api-token"}  # matches cfg fixture's PKM_SIDECAR_API_TOKEN


def test_status_requires_auth(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/api/status")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_status_rejects_wrong_token(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_status_ok_with_auth(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/api/status", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "pkm-sidecar"
    assert body["version"] == __version__
    assert body["joplin"]["configured"] is True
    # reachable depends on whether a real Joplin is up — just assert it's a bool.
    assert isinstance(body["joplin"]["reachable"], bool)
    assert set(body["database"]) == {
        "path",
        "note_count",
        "folder_count",
        "tag_count",
        "task_count",
        "resource_count",
    }
    assert set(body["indexing"]) == {
        "last_full_index_at",
        "last_incremental_index_at",
        "last_event_id",
    }


def test_status_never_contains_joplin_token(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/api/status", headers=AUTH)
    assert "tok" not in resp.text.replace("test-api-token", "")  # the joplin token value "tok"
    # structurally, there is no token field anywhere in the joplin block
    assert "token" not in resp.json()["joplin"]


def test_api_security_headers(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/api/status", headers=AUTH)
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"


def test_status_reflects_indexed_data(cfg: AppConfig) -> None:
    # Seed the index directly, then confirm /api/status counts it.
    from pkm_sidecar import db
    from pkm_sidecar.repositories import NoteRepository

    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    repo = NoteRepository(conn)
    with repo.transaction():
        repo.upsert_note({"id": "n1", "title": "T", "body": "b"}, indexed_at=1)
    conn.close()

    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/api/status", headers=AUTH)
    assert resp.json()["database"]["note_count"] == 1
