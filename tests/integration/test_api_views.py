"""Tests for the workflow view + note-detail endpoints (PRD §13.2, §18.5/6)."""

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar import db, services
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig
from tests._fake_joplin import FakeJoplin

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def seeded(cfg: AppConfig):
    """Build an app whose index is pre-populated from a fake Joplin rebuild."""

    async def _seed() -> None:
        fake = FakeJoplin()
        fake.add_folder("f1", "Inbox")
        fake.add_note("n1", "Recent note", "body about RabbitMQ", parent_id="f1", updated_time=200)
        fake.add_note("n2", "Older untagged", "- [ ] do a thing", updated_time=100)
        fake.add_tag("t1", "project", note_ids=("n1",))
        db.init_db(cfg.database.path)
        conn = db.open_writer_connection(cfg.database.path)
        try:
            async with fake.client() as client:
                await services.full_rebuild(conn, client, cfg)
        finally:
            conn.close()

    import asyncio

    asyncio.run(_seed())
    with TestClient(create_app(cfg, start_indexer_loop=False)) as c:
        yield c


def test_recent_ordered_desc(seeded: TestClient) -> None:
    resp = seeded.get("/api/notes/recent", headers=AUTH)
    assert resp.status_code == 200
    assert [n["id"] for n in resp.json()] == ["n1", "n2"]
    # list views never carry the body
    assert "body" not in resp.json()[0]


def test_inbox(seeded: TestClient) -> None:
    ids = {n["id"] for n in seeded.get("/api/notes/inbox", headers=AUTH).json()}
    assert ids == {"n1"}


def test_untagged(seeded: TestClient) -> None:
    ids = {n["id"] for n in seeded.get("/api/notes/untagged", headers=AUTH).json()}
    assert ids == {"n2"}  # n1 is tagged


def test_todos(seeded: TestClient) -> None:
    ids = {n["id"] for n in seeded.get("/api/notes/todos", headers=AUTH).json()}
    assert ids == {"n2"}  # markdown task


def test_note_detail_includes_body(seeded: TestClient) -> None:
    resp = seeded.get("/api/note/n1", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["body"] == "body about RabbitMQ"


def test_note_detail_404(seeded: TestClient) -> None:
    resp = seeded.get("/api/note/missing", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_note_tasks(seeded: TestClient) -> None:
    resp = seeded.get("/api/note/n2/tasks", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["checked"] is False


def test_stale_limit_validation(seeded: TestClient) -> None:
    assert seeded.get("/api/notes/recent?limit=0", headers=AUTH).status_code == 422
    assert seeded.get("/api/notes/recent?limit=9999", headers=AUTH).status_code == 422
