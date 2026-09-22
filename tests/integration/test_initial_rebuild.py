"""Tests for first-run backfill decision (PRD §11; v0.2.1 launcher gap fix)."""

import pytest

from pkm_sidecar import db, services
from pkm_sidecar.config import AppConfig, load_config


def _cfg(tmp_path, monkeypatch, **env) -> AppConfig:
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text("")
    base = {
        "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
        "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
    }
    base.update(env)
    return load_config(env=base)


@pytest.fixture
def writer(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, JOPLIN_TOKEN="tok")
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    yield cfg, conn
    conn.close()


def test_empty_index_with_token_triggers_rebuild(writer) -> None:
    cfg, conn = writer
    assert services.needs_initial_rebuild(cfg, conn) is True


def test_after_full_index_does_not_trigger(writer) -> None:
    cfg, conn = writer
    db.set_meta(conn, db.META_LAST_FULL_INDEX_AT, "1700000000")
    assert services.needs_initial_rebuild(cfg, conn) is False


def test_no_token_does_not_trigger(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)  # no JOPLIN_TOKEN
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    try:
        assert services.needs_initial_rebuild(cfg, conn) is False
    finally:
        conn.close()


def test_flag_off_does_not_trigger(tmp_path, monkeypatch) -> None:
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text("[indexing]\nrebuild_on_empty_start = false\n")
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    cfg = load_config(
        env={
            "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
            "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
            "JOPLIN_TOKEN": "tok",
        }
    )
    assert cfg.indexing.rebuild_on_empty_start is False
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    try:
        assert services.needs_initial_rebuild(cfg, conn) is False
    finally:
        conn.close()


class TestInterruptedRebuildRecovery:
    """A rebuild that never finished must be redone, not silently served.

    ``reset_derived`` wipes tasks/links/tags up front and only the *final*
    transaction stamps ``last_full_index_at``. Kill the process in between and
    that stamp still holds the previous run's value, so nothing noticed: the
    views quietly returned partial data ("Untagged" listing every note, "TODOs"
    empty) until somebody ran a rebuild by hand.
    """

    def test_interrupted_rebuild_is_detected(self, writer) -> None:
        cfg, conn = writer
        db.set_meta(conn, db.META_LAST_FULL_INDEX_AT, "1700000000")  # an earlier good run
        assert services.rebuild_reason(cfg, conn) is None

        db.reset_derived(conn, started_at=1700000123)  # …then a rebuild starts and dies
        assert services.rebuild_reason(cfg, conn) == "interrupted_rebuild"
        # The old stamp is exactly why nothing else spots it.
        assert services.needs_initial_rebuild(cfg, conn) is False

    def test_recovery_is_not_gated_on_rebuild_on_empty_start(self, tmp_path, monkeypatch) -> None:
        """That option skips backfilling a fresh index, not repairing a broken one."""
        cfg = _cfg(tmp_path, monkeypatch, JOPLIN_TOKEN="tok")
        cfg = cfg.model_copy(
            update={"indexing": cfg.indexing.model_copy(update={"rebuild_on_empty_start": False})}
        )
        db.init_db(cfg.database.path)
        conn = db.open_writer_connection(cfg.database.path)
        try:
            db.set_meta(conn, db.META_LAST_FULL_INDEX_AT, "1700000000")
            db.reset_derived(conn, started_at=1700000123)
            assert services.rebuild_reason(cfg, conn) == "interrupted_rebuild"
        finally:
            conn.close()

    def test_no_token_means_no_rebuild(self, tmp_path, monkeypatch) -> None:
        cfg = _cfg(tmp_path, monkeypatch)  # no JOPLIN_TOKEN
        db.init_db(cfg.database.path)
        conn = db.open_writer_connection(cfg.database.path)
        try:
            db.reset_derived(conn, started_at=1700000123)
            assert services.rebuild_reason(cfg, conn) is None  # nothing to rebuild from
        finally:
            conn.close()

    def test_empty_index_still_reports_backfill(self, writer) -> None:
        cfg, conn = writer
        assert services.rebuild_reason(cfg, conn) == "empty_index_backfill"
