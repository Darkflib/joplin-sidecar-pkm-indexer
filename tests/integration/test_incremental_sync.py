"""Integration test: incremental sync against a mocked Joplin (PRD §18.8)."""

import sqlite3

import pytest

from pkm_sidecar import db, services
from pkm_sidecar.config import AppConfig
from pkm_sidecar.errors import JoplinError
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


async def test_idle_ticks_write_no_index_run_rows(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    """A tick with no events must not leave an index_runs row behind.

    At the default 10s poll an idle sidecar ticks ~8,640 times a day; recording
    each one grew the table without bound (nothing ever reads or prunes it).
    """
    fake = FakeJoplin()
    fake.add_note("n1", "Title", "body")
    repo = NoteRepository(writer)

    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        rows_after_rebuild = writer.execute("SELECT count(*) FROM index_runs").fetchone()[0]

        for _ in range(10):  # ten consecutive ticks with nothing to do
            result = await services.incremental_sync_once(writer, client, cfg)
            assert result.status == "success"
            assert result.run_id is None  # no row was opened

    assert writer.execute("SELECT count(*) FROM index_runs").fetchone()[0] == rows_after_rebuild
    # …but the dashboard's "synced N ago" still tracks the poll.
    assert repo.get_meta(db.META_LAST_INCREMENTAL_INDEX_AT) is not None


async def test_tick_with_work_still_records_a_run(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "Title", "body")

    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        before = writer.execute("SELECT count(*) FROM index_runs").fetchone()[0]
        fake.notes["n1"]["body"] = "changed"
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)
        result = await services.incremental_sync_once(writer, client, cfg)

    assert result.run_id is not None
    assert writer.execute("SELECT count(*) FROM index_runs").fetchone()[0] == before + 1
    row = writer.execute(
        "SELECT mode, status, notes_seen, completed_at FROM index_runs WHERE id = ?",
        (result.run_id,),
    ).fetchone()
    assert (row["mode"], row["status"], row["notes_seen"]) == ("incremental", "success", 1)
    assert row["completed_at"] is not None


async def test_failed_tick_is_recorded_but_bounded(
    cfg: AppConfig, writer: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failures are worth keeping, so they are recorded — but retention caps them."""
    monkeypatch.setattr(services, "INDEX_RUN_RETENTION", 5)
    fake = FakeJoplin(events_status=500)  # /events is broken for every tick

    async with fake.client() as client:
        for _ in range(20):
            with pytest.raises(JoplinError):
                await services.incremental_sync_once(writer, client, cfg)

    rows = writer.execute("SELECT count(*) FROM index_runs").fetchone()[0]
    assert rows == 5  # capped, not 20
    assert (
        writer.execute("SELECT count(*) FROM index_runs WHERE status = 'failed'").fetchone()[0] == 5
    )


async def test_retention_keeps_the_most_recent_runs(writer: sqlite3.Connection) -> None:
    for _ in range(services.INDEX_RUN_RETENTION + 25):
        run_id, _ = services.start_run_record(writer, "incremental")
        services._finish_run(
            writer, services.IndexRunResult(run_id=run_id, mode="incremental", status="success")
        )

    kept = [r["id"] for r in writer.execute("SELECT id FROM index_runs ORDER BY id").fetchall()]
    assert len(kept) == services.INDEX_RUN_RETENTION
    assert kept[-1] == services.INDEX_RUN_RETENTION + 25  # newest survived
    assert kept[0] == 26  # oldest 25 were trimmed
