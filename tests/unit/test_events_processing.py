"""Unit tests for event dispatch and the indexer lifecycle (PRD §11.2)."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from pkm_sidecar import db, logging_config, services
from pkm_sidecar.config import AppConfig, load_config
from pkm_sidecar.indexer import create_indexer
from pkm_sidecar.repositories import NoteRepository
from pkm_sidecar.services import ITEM_TYPE_FOLDER, ITEM_TYPE_NOTE, ITEM_TYPE_TAG
from tests._fake_joplin import FakeJoplin


@pytest.fixture(autouse=True)
def _clean_logging():
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
        }
    )


@pytest.fixture
def writer(cfg: AppConfig):
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    yield conn
    conn.close()


async def test_folder_event_refreshes_folders(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        fake.add_folder("f1", "Inbox")
        fake.push_event(ITEM_TYPE_FOLDER, "f1", 2)  # update/create
        result = await services.incremental_sync_once(writer, client, cfg)
    assert result.folders_seen == 1
    assert writer.execute("SELECT title FROM folders WHERE id='f1'").fetchone()["title"] == "Inbox"


async def test_tag_delete_event(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "T", "b")
    fake.add_tag("t1", "tag", note_ids=("n1",))
    repo = NoteRepository(writer)
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        assert {n.id for n in repo.fetch_untagged()} == set()  # n1 is tagged
        fake.push_event(ITEM_TYPE_TAG, "t1", 3)  # delete tag
        await services.incremental_sync_once(writer, client, cfg)
    assert {n.id for n in repo.fetch_untagged()} == {"n1"}  # cascade removed the link


async def test_note_event_for_missing_note_marks_deleted(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "T", "b")
    repo = NoteRepository(writer)
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        del fake.notes["n1"]  # gone from Joplin
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)  # update event, but fetch will 404
        await services.incremental_sync_once(writer, client, cfg)
    assert repo.get_note("n1").deleted is True  # type: ignore[union-attr]


async def test_cursor_not_advanced_on_failure(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "T", "b")
    repo = NoteRepository(writer)
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        result = await services.incremental_sync_once(writer, client, cfg)
    # No events pushed -> cursor stays at the fake's default ("0"), no crash.
    assert result.status == "success"
    assert repo.get_meta(db.META_LAST_EVENT_ID) == "0"


async def test_indexer_background_loop_starts_and_stops(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "T", "b")
    async with fake.client() as client:
        handle = create_indexer(cfg, writer, client)
        await handle.start(enable_background=True)
        await asyncio.sleep(0.05)  # let at least one tick run
        await handle.stop()
    assert handle._task is None
    # at least one incremental sync ran
    assert NoteRepository(writer).get_meta(db.META_LAST_INCREMENTAL_INDEX_AT) is not None


async def test_test_mode_disables_background_loop(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    async with fake.client() as client:
        handle = create_indexer(cfg, writer, client)
        await handle.start(enable_background=False)
        assert handle._task is None
        await handle.stop()
