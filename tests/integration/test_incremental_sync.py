"""Integration test: incremental sync against a mocked Joplin (PRD §18.8)."""

import sqlite3

from pkm_sidecar import db, services
from pkm_sidecar.config import AppConfig
from pkm_sidecar.indexer import create_indexer
from pkm_sidecar.repositories import NoteRepository
from pkm_sidecar.services import ITEM_TYPE_NOTE
from tests._fake_joplin import FakeJoplin


async def test_incremental_updates_changed_note(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "Title", "original kafka content")
    repo = NoteRepository(writer)

    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        before = repo.get_note("n1").body_hash  # type: ignore[union-attr]

        # Edit the note in Joplin and emit an update event.
        fake.notes["n1"]["body"] = "updated rabbitmq content"
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)
        result = await services.incremental_sync_once(writer, client, cfg)

    assert result.status == "success"
    assert repo.get_note("n1").body_hash != before  # body_hash changed (PRD §18.8)
    assert [h.id for h in repo.fts_search("rabbitmq")] == ["n1"]  # new content searchable
    assert repo.get_meta(db.META_LAST_EVENT_ID) == "1"  # cursor advanced


async def test_incremental_refreshes_tag_membership(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    """v0.2: a tag added in Joplin shows up after incremental sync (no rebuild)."""
    fake = FakeJoplin()
    fake.add_note("n1", "Title", "body")
    repo = NoteRepository(writer)
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        assert {n.id for n in repo.fetch_untagged()} == {"n1"}  # starts untagged

        # Tag the note in Joplin, emit a note-update event, sync.
        fake.add_tag("t1", "project", note_ids=("n1",))
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)
        await services.incremental_sync_once(writer, client, cfg)

    assert {n.id for n in repo.fetch_untagged()} == set()  # n1 is now tagged
    assert writer.execute("SELECT count(*) FROM note_tags WHERE note_id='n1'").fetchone()[0] == 1


async def test_incremental_deletes_note(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "Title", "body")
    repo = NoteRepository(writer)
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        fake.push_event(ITEM_TYPE_NOTE, "n1", 3)  # delete
        await services.incremental_sync_once(writer, client, cfg)
    assert repo.get_note("n1").deleted is True  # type: ignore[union-attr]
    assert repo.fetch_recent() == []


async def test_cursor_invalid_demotes_to_full_rebuild(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin(events_status=400)  # /events returns 400 -> cursor invalid
    fake.add_note("n1", "Title", "body")
    repo = NoteRepository(writer)
    async with fake.client() as client:
        handle = create_indexer(cfg, writer, client)
        # seed a stale cursor so the next sync hits the invalid-cursor path
        repo.set_meta(db.META_LAST_EVENT_ID, "999")
        result = await handle.run_incremental_once()
    assert result.message == "cursor invalid → rebuilt"
    assert {n.id for n in repo.fetch_recent()} == {"n1"}
    assert repo.get_meta(db.META_LAST_EVENT_ID) is None  # cursor cleared by the rebuild


async def test_incremental_issues_only_get_requests(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "T", "b")
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        fake.notes["n1"]["body"] = "changed"
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)
        await services.incremental_sync_once(writer, client, cfg)
    assert fake.only_get_requests()
