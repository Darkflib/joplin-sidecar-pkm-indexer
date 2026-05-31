"""Shared fixtures for integration tests: tmp config, writer connection."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pkm_sidecar import db, logging_config
from pkm_sidecar.config import AppConfig, load_config


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    logging_config.reset_for_tests()
    yield
    logging_config.reset_for_tests()


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text("")
    return load_config(
        env={
            "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
            "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
            "JOPLIN_TOKEN": "tok",
            "PKM_SIDECAR_API_TOKEN": "test-api-token",
        }
    )


@pytest.fixture
def writer(cfg: AppConfig) -> Iterator[sqlite3.Connection]:
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    yield conn
    conn.close()
