"""Tests for degraded-mode handling in the dashboard (PRD §17.1, §18.9).

Full degraded rendering is JS behaviour exercised by the manual checklist (§19.3);
here we assert the server contract that makes it possible: /api/status still
answers (with stale data) when Joplin is unreachable, and the client asset carries
the offline-badge logic.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar import db
from pkm_sidecar.app import create_app
from pkm_sidecar.config import load_config
from pkm_sidecar.repositories import NoteRepository

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def client_with_stale_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text("")
    # Point Joplin at a dead port so reachability probes fail fast.
    cfg = load_config(
        env={
            "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
            "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
            "PKM_SIDECAR_API_TOKEN": "test-api-token",
            "JOPLIN_TOKEN": "tok",
            "JOPLIN_BASE_URL": "http://127.0.0.1:9",
        }
    )
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    repo = NoteRepository(conn)
    with repo.transaction():
        repo.upsert_note({"id": "n1", "title": "Stale note", "body": "b"}, indexed_at=1)
    conn.close()
    with TestClient(create_app(cfg, start_indexer_loop=False)) as c:
        yield c


def test_status_serves_stale_index_when_joplin_unreachable(
    client_with_stale_index: TestClient,
) -> None:
    resp = client_with_stale_index.get("/api/status", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["joplin"]["reachable"] is False
    assert body["database"]["note_count"] == 1  # stale index still available (PRD §18.9)


def test_dashboard_loads_when_joplin_down(client_with_stale_index: TestClient) -> None:
    assert client_with_stale_index.get("/").status_code == 200


def test_js_paints_offline_badge() -> None:
    from pkm_sidecar.dashboard import STATIC_DIR

    js = (STATIC_DIR / "dashboard.js").read_text()
    assert "reachable" in js
    assert "Joplin offline" in js
    assert "degraded" in js
