"""Review API for suggestions (docs/enrichment.md §7).

The whole surface is read-plus-decide. Accepting records a decision and writes
nothing to Joplin — the apply path is a separate increment — so these tests also
pin that property rather than leaving it to the docstring.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig, load_config, suggestions_db_path
from pkm_sidecar.enrichment import db as edb
from pkm_sidecar.enrichment.models import Suggestion
from pkm_sidecar.enrichment.repository import SuggestionRepository, compute_input_hash

AUTH = {"Authorization": "Bearer test-api-token"}


def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> AppConfig:
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text(
        f"[enrichment]\nenabled = {'true' if enabled else 'false'}\n"
    )
    return load_config(
        env={
            "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
            "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
            "PKM_SIDECAR_API_TOKEN": "test-api-token",
            "JOPLIN_BASE_URL": "http://127.0.0.1:9",
        }
    )


def _seed(cfg: AppConfig, *, titles: int = 1) -> list[int]:
    path = suggestions_db_path(cfg)
    edb.init_db(path)
    conn = edb.open_connection(path)
    repo = SuggestionRepository(conn)
    ids: list[int] = []
    try:
        with repo.transaction():
            for i in range(titles):
                stored = repo.record(
                    Suggestion(
                        note_id=f"n{i}",
                        kind="title",
                        input_hash=compute_input_hash(
                            kind="title",
                            body_hash=f"bh{i}",
                            current_state="Untitled",
                            model="m",
                            prompt_version=1,
                        ),
                        payload={"title": f"Proposed Title {i}"},
                        current_value={"title": "Untitled"},
                        note_body_hash=f"bh{i}",
                        model="m",
                        prompt_version=1,
                        reason="joplin_default",
                        confidence=0.5 + i / 100,
                    )
                )
                ids.append(stored.id or 0)
    finally:
        conn.close()
    return ids


@pytest.fixture
def enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(tmp_path, monkeypatch, enabled=True)
    ids = _seed(cfg, titles=3)
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        yield client, cfg, ids


@pytest.fixture
def disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(tmp_path, monkeypatch, enabled=False)
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        yield client, cfg


class TestListing:
    def test_requires_auth(self, enabled) -> None:
        client, _, _ = enabled
        assert client.get("/api/suggestions").status_code == 401

    def test_lists_pending_best_first(self, enabled) -> None:
        client, _, _ = enabled
        body = client.get("/api/suggestions", headers=AUTH).json()
        assert [s["proposed"]["title"] for s in body] == [
            "Proposed Title 2",
            "Proposed Title 1",
            "Proposed Title 0",
        ]

    def test_carries_the_comparison_and_the_reason(self, enabled) -> None:
        client, _, _ = enabled
        first = client.get("/api/suggestions", headers=AUTH).json()[0]
        assert first["current"] == {"title": "Untitled"}
        assert first["reason"] == "joplin_default"
        assert first["model"] == "m"

    def test_kind_filter_is_validated(self, enabled) -> None:
        client, _, _ = enabled
        assert client.get("/api/suggestions?kind=title", headers=AUTH).status_code == 200
        assert client.get("/api/suggestions?kind=nonsense", headers=AUTH).status_code == 422


class TestDeciding:
    def test_accept_records_and_removes_from_the_queue(self, enabled) -> None:
        client, _, ids = enabled
        resp = client.post(f"/api/suggestions/{ids[0]}/accept", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"id": ids[0], "decision": "accepted", "wrote_to_joplin": False}
        remaining = [s["id"] for s in client.get("/api/suggestions", headers=AUTH).json()]
        assert ids[0] not in remaining

    def test_accept_writes_nothing_to_joplin(self, enabled) -> None:
        """The property that makes these buttons safe to click at all."""
        client, _, ids = enabled
        assert (
            client.post(f"/api/suggestions/{ids[0]}/accept", headers=AUTH).json()["wrote_to_joplin"]
            is False
        )

    def test_reject_records_a_decision(self, enabled) -> None:
        client, _, ids = enabled
        assert (
            client.post(f"/api/suggestions/{ids[1]}/reject", headers=AUTH).json()["decision"]
            == "rejected"
        )

    def test_unknown_id_is_404(self, enabled) -> None:
        client, _, _ = enabled
        assert client.post("/api/suggestions/99999/accept", headers=AUTH).status_code == 404

    def test_dismiss_opts_the_note_out_for_good(self, enabled) -> None:
        """Distinct from rejecting once: this must outlive a body edit."""
        client, cfg, ids = enabled
        resp = client.post(f"/api/suggestions/{ids[0]}/dismiss", headers=AUTH)
        assert resp.status_code == 202
        conn = edb.open_connection(suggestions_db_path(cfg))
        try:
            assert SuggestionRepository(conn).is_opted_out("n0", "title") is True
        finally:
            conn.close()


class TestStatusIntegration:
    def test_status_reports_pending_counts(self, enabled) -> None:
        client, _, _ = enabled
        enrichment = client.get("/api/status", headers=AUTH).json()["enrichment"]
        assert enrichment == {"enabled": True, "pending_titles": 3, "pending_tags": 0}

    def test_counts_drop_as_you_review(self, enabled) -> None:
        client, _, ids = enabled
        client.post(f"/api/suggestions/{ids[0]}/reject", headers=AUTH)
        assert client.get("/api/status", headers=AUTH).json()["enrichment"]["pending_titles"] == 2


class TestDisabled:
    def test_listing_is_empty_rather_than_an_error(self, disabled) -> None:
        """So the dashboard hides its column without special-casing a failure."""
        client, _ = disabled
        resp = client.get("/api/suggestions", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_status_says_disabled(self, disabled) -> None:
        client, _ = disabled
        assert client.get("/api/status", headers=AUTH).json()["enrichment"]["enabled"] is False

    def test_deciding_is_404(self, disabled) -> None:
        client, _ = disabled
        assert client.post("/api/suggestions/1/accept", headers=AUTH).status_code == 404

    def test_no_store_file_is_created(self, disabled) -> None:
        """A disabled feature must not grow a database."""
        client, cfg = disabled
        client.get("/api/suggestions", headers=AUTH)
        assert not suggestions_db_path(cfg).exists()


class TestDashboardAsset:
    def test_the_review_column_and_its_safety_note_ship(self) -> None:
        from pkm_sidecar.dashboard import STATIC_DIR, TEMPLATES_DIR

        html = (TEMPLATES_DIR / "index.html").read_text()
        js = (STATIC_DIR / "dashboard.js").read_text()
        assert 'id="col-suggestions"' in html
        assert "loadSuggestions" in js
        assert "/api/suggestions" in js
        # The button tooltips state that nothing is written to Joplin.
        assert "nothing is written to Joplin" in js
