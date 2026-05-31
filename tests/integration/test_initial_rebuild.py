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
