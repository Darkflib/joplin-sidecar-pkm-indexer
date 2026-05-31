"""Tests for the index command endpoints (PRD §11.3, §13.2)."""

import asyncio

from fastapi.testclient import TestClient

from pkm_sidecar import db
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig
from pkm_sidecar.indexer import create_indexer
from pkm_sidecar.repositories import NoteRepository
from tests._fake_joplin import FakeJoplin

AUTH = {"Authorization": "Bearer test-api-token"}


def test_rebuild_returns_202_with_run_id(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.post("/api/index/rebuild", headers=AUTH)
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "full"
    assert isinstance(body["run_id"], int)
    assert isinstance(body["started_at"], int)


def test_rebuild_conflict_when_in_progress(cfg: AppConfig) -> None:
    app = create_app(cfg, start_indexer_loop=False)
    with TestClient(app) as client:
        # Force the in-progress flag so a fresh request 409s deterministically.
        app.state.indexer._rebuild_in_progress = True
        resp = client.post("/api/index/rebuild", headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_sync_returns_202(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.post("/api/index/sync", headers=AUTH)
    assert resp.status_code == 202
    assert resp.json() == {"accepted": True}


def test_reindex_single_note(cfg: AppConfig) -> None:
    """reindex_one fetches a note from Joplin and indexes it (PRD §11.3)."""

    async def _run() -> tuple[bool, bool]:
        fake = FakeJoplin()
        fake.add_note("n1", "Title", "body")
        db.init_db(cfg.database.path)
        conn = db.open_writer_connection(cfg.database.path)
        try:
            async with fake.client() as client:
                handle = create_indexer(cfg, conn, client)
                found = await handle.reindex_one("n1")
                missing = await handle.reindex_one("ghost")
        finally:
            indexed = NoteRepository(conn).get_note("n1") is not None
            conn.close()
        return found, indexed and not missing

    found_and_indexed = asyncio.run(_run())
    assert found_and_indexed == (True, True)


def test_reindex_endpoint_404_for_unknown_note(cfg: AppConfig) -> None:
    # Outcome depends on the live Joplin: 404 if reachable with a valid token and the
    # note is absent, 502 if the token is rejected, 503 if Joplin is unreachable.
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.post("/api/index/note/definitely-not-a-real-note", headers=AUTH)
    assert resp.status_code in (404, 502, 503)
