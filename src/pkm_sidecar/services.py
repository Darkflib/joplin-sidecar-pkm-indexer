"""Indexing orchestration: convert Joplin payloads into derived rows (PRD §11).

These are the lock-free cores. The indexer module owns the single writer
connection, the rebuild/incremental serialisation lock, and the background loop,
and calls into here. All DB writes funnel through :class:`NoteRepository`.

Behavioural notes:

* **Full rebuild always re-extracts** tasks/links for every note. ``reset_derived``
  wipes the derived tables first, and a body-hash short-circuit would otherwise
  skip unchanged notes and lose their tasks/links. The body-changed gate is used
  only by incremental :func:`reindex_note`.
* **``note_tags`` are populated only by full rebuild** (via ``get_tag_notes``); the
  read-only client has no note→tags endpoint, so incremental note events do not
  refresh tag membership. Cross-note tag drift is resolved at the next rebuild
  (documented v0.1 limitation).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from pkm_sidecar import db, markdown_extract
from pkm_sidecar.config import AppConfig
from pkm_sidecar.errors import JoplinCursorInvalidError, JoplinNotFoundError
from pkm_sidecar.joplin_client import JoplinClient
from pkm_sidecar.logging_config import get_logger, log_event
from pkm_sidecar.repositories import NoteRepository

logger = get_logger("indexer")

NOTE_FIELDS = [
    "id",
    "parent_id",
    "title",
    "body",
    "created_time",
    "updated_time",
    "user_created_time",
    "user_updated_time",
    "is_todo",
    "todo_due",
    "todo_completed",
    "source_url",
]
FOLDER_FIELDS = ["id", "parent_id", "title", "created_time", "updated_time"]
TAG_FIELDS = ["id", "title", "created_time", "updated_time"]
RESOURCE_FIELDS = [
    "id",
    "title",
    "mime",
    "filename",
    "file_extension",
    "size",
    "created_time",
    "updated_time",
]

# Joplin model and change-type constants (Joplin Data API /events).
ITEM_TYPE_NOTE = 1
ITEM_TYPE_FOLDER = 2
ITEM_TYPE_RESOURCE = 4
ITEM_TYPE_TAG = 5
CHANGE_DELETE = 3

MAX_EVENTS_PER_TICK = 1000


@dataclass
class IndexRunResult:
    run_id: int
    mode: Literal["full", "incremental", "single_note"]
    status: Literal["success", "partial", "failed"]
    notes_seen: int = 0
    notes_updated: int = 0
    folders_seen: int = 0
    tags_seen: int = 0
    errors: int = 0
    message: str | None = None


def compute_body_hash(body: str) -> str:
    """sha256 hex of the UTF-8 body (matches repositories' upsert hashing)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def needs_initial_rebuild(cfg: AppConfig, conn: sqlite3.Connection) -> bool:
    """True if the index has never been fully built and a backfill should run.

    The serve-only (launcher) path otherwise only runs incremental sync, which
    captures changes from the cursor forward and never backfills existing notes.
    """
    return (
        cfg.indexing.rebuild_on_empty_start
        and cfg.joplin.token is not None
        and db.get_meta(conn, db.META_LAST_FULL_INDEX_AT) is None
    )


def _now() -> int:
    """Epoch seconds — for human-facing meta timestamps and index_runs."""
    return int(time.time())


def _marker() -> int:
    """A strictly-increasing stamp for ``indexed_at`` so sweep_orphans is exact.

    Wall-clock seconds collide when two rebuilds run in the same second, which
    would let ``indexed_at < run_marker`` miss real orphans. Nanoseconds don't.
    """
    return time.time_ns()


def _start_run(conn: sqlite3.Connection, mode: str, started_at: int) -> int:
    cur = conn.execute(
        "INSERT INTO index_runs(started_at, mode, status) VALUES (?, ?, 'running')",
        (started_at, mode),
    )
    return int(cur.lastrowid or 0)


def start_run_record(conn: sqlite3.Connection, mode: str) -> tuple[int, int]:
    """Create the index_runs row up front so the API can return its id in a 202."""
    started_at = _now()
    return _start_run(conn, mode, started_at), started_at


def _finish_run(conn: sqlite3.Connection, result: IndexRunResult) -> None:
    conn.execute(
        "UPDATE index_runs SET completed_at=?, status=?, notes_seen=?, notes_updated=?, "
        "folders_seen=?, tags_seen=?, errors=?, message=? WHERE id=?",
        (
            _now(),
            result.status,
            result.notes_seen,
            result.notes_updated,
            result.folders_seen,
            result.tags_seen,
            result.errors,
            result.message,
            result.run_id,
        ),
    )


async def _chunked(
    aiter: AsyncIterator[dict[str, Any]], size: int
) -> AsyncIterator[list[dict[str, Any]]]:
    """Buffer an async iterator into lists of at most *size* items.

    Lets us hold the write transaction only while writing a chunk (sync), never
    while awaiting the next network page.
    """
    buf: list[dict[str, Any]] = []
    async for item in aiter:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


# --- single-note primitive -------------------------------------------------


async def reindex_note(
    conn: sqlite3.Connection, client: JoplinClient, note_id: str, *, indexed_at: int | None = None
) -> bool:
    """Fetch one note and upsert it, re-extracting tasks/links iff the body changed.

    Returns True if the note was found and indexed, False if Joplin no longer has
    it (in which case it is marked deleted). The network fetch happens outside the
    transaction; all writes are in a single BEGIN IMMEDIATE.
    """
    indexed_at = _marker() if indexed_at is None else indexed_at
    repo = NoteRepository(conn)
    try:
        note = await client.get_note(note_id, fields=NOTE_FIELDS)
    except JoplinNotFoundError:
        with repo.transaction():
            repo.mark_note_deleted(note_id)
        return False
    # Refresh this note's tag membership directly (v0.2): incremental events no
    # longer wait for a full rebuild to pick up tag changes. Fetched outside the txn.
    note_tags = [t async for t in client.get_note_tags(note_id, fields=["id", "title"])]
    with repo.transaction():
        body_changed = repo.upsert_note(note, indexed_at=indexed_at)
        if body_changed:
            extracted = markdown_extract.extract(note_id, note.get("body") or "")
            repo.replace_tasks_for_note(note_id, extracted["tasks"], indexed_at=indexed_at)
            repo.replace_links_for_note(note_id, extracted["links"], indexed_at=indexed_at)
            # Refresh embedded-resource links for this note (v0.2).
            repo.rebuild_note_resources_for_note(note_id, indexed_at=indexed_at)
        for tag in note_tags:
            repo.upsert_tag(tag, indexed_at=indexed_at)
        repo.replace_note_tags(note_id, [t["id"] for t in note_tags], indexed_at=indexed_at)
    return True


# --- full rebuild ----------------------------------------------------------


async def full_rebuild(
    conn: sqlite3.Connection, client: JoplinClient, cfg: AppConfig, *, run_id: int | None = None
) -> IndexRunResult:
    """Rebuild the whole index from Joplin (PRD §11.1).

    If *run_id* is given (the API pre-created the row to return in its 202), reuse
    it; otherwise create the index_runs row here.
    """
    started_at = _now()
    run_started = _marker()  # ns stamp for indexed_at / sweep_orphans
    repo = NoteRepository(conn)
    if run_id is None:
        run_id = _start_run(conn, "full", started_at)
    result = IndexRunResult(run_id=run_id, mode="full", status="success")
    log_event(logger, "index.full.started", run_id=run_id)
    page = cfg.joplin.page_limit

    try:
        db.reset_derived(conn)

        # Folders.
        async for chunk in _chunked(client.get_folders(fields=FOLDER_FIELDS), page):
            with repo.transaction():
                for folder in chunk:
                    repo.upsert_folder(folder, indexed_at=run_started)
                    result.folders_seen += 1

        # Notes (always re-extract derived data on a full rebuild).
        async for chunk in _chunked(
            client.get_notes(fields=NOTE_FIELDS, order_by="updated_time", order_dir="ASC"), page
        ):
            with repo.transaction():
                for note in chunk:
                    changed = repo.upsert_note(note, indexed_at=run_started)
                    extracted = markdown_extract.extract(note["id"], note.get("body") or "")
                    repo.replace_tasks_for_note(
                        note["id"], extracted["tasks"], indexed_at=run_started
                    )
                    repo.replace_links_for_note(
                        note["id"], extracted["links"], indexed_at=run_started
                    )
                    result.notes_seen += 1
                    if changed:
                        result.notes_updated += 1

        # Tags.
        tag_ids: list[str] = []
        async for chunk in _chunked(client.get_tags(fields=TAG_FIELDS), page):
            with repo.transaction():
                for tag in chunk:
                    repo.upsert_tag(tag, indexed_at=run_started)
                    tag_ids.append(tag["id"])
                    result.tags_seen += 1

        # Note-tag relationships, rebuilt from each tag's notes.
        for tag_id in tag_ids:
            async for chunk in _chunked(client.get_tag_notes(tag_id, fields=["id"]), page):
                with repo.transaction():
                    for note in chunk:
                        repo.upsert_note_tag(note["id"], tag_id, indexed_at=run_started)

        # Resources (metadata only); note_resources is derived from links ∩ resources.
        async for chunk in _chunked(client.get_resources(fields=RESOURCE_FIELDS), page):
            with repo.transaction():
                for resource in chunk:
                    repo.upsert_resource(resource, indexed_at=run_started)
        with repo.transaction():
            repo.rebuild_all_note_resources(indexed_at=run_started)

        with repo.transaction():
            repo.sweep_orphans(run_started)
            repo.set_meta(db.META_LAST_FULL_INDEX_AT, str(started_at))

        log_event(
            logger,
            "index.full.completed",
            run_id=run_id,
            notes=result.notes_seen,
            folders=result.folders_seen,
            tags=result.tags_seen,
        )
    except Exception as exc:
        result.status = "failed"
        result.errors += 1
        result.message = f"{type(exc).__name__}: {exc}"
        log_event(logger, "index.full.failed", level=40, run_id=run_id, error=type(exc).__name__)
        _finish_run(conn, result)
        raise
    _finish_run(conn, result)
    return result


# --- incremental sync ------------------------------------------------------


async def _apply_note_event(
    conn: sqlite3.Connection, client: JoplinClient, note_id: str, change: int, indexed_at: int
) -> bool:
    repo = NoteRepository(conn)
    if change == CHANGE_DELETE:
        with repo.transaction():
            repo.mark_note_deleted(note_id)
        return True
    return await reindex_note(conn, client, note_id, indexed_at=indexed_at)


async def incremental_sync_once(
    conn: sqlite3.Connection, client: JoplinClient, cfg: AppConfig
) -> IndexRunResult:
    """Apply one batch of Joplin events (PRD §11.2).

    Advances the cursor only after the whole batch is applied. On an invalid
    cursor the cursor meta is cleared and :class:`JoplinCursorInvalidError` is
    re-raised so the caller can demote to a full rebuild (PRD §17.4).
    """
    started_at = _now()
    run_started = _marker()  # ns stamp for indexed_at
    repo = NoteRepository(conn)
    cursor = repo.get_meta(db.META_LAST_EVENT_ID)
    run_id = _start_run(conn, "incremental", started_at)
    result = IndexRunResult(run_id=run_id, mode="incremental", status="success")
    log_event(logger, "index.incremental.started", run_id=run_id)

    needs_folder_refresh = False
    needs_tag_refresh = False
    needs_resource_refresh = False
    try:
        try:
            batch = await client.get_events(cursor=cursor, limit=MAX_EVENTS_PER_TICK)
        except JoplinCursorInvalidError:
            with repo.transaction():
                conn.execute("DELETE FROM meta WHERE key = ?", (db.META_LAST_EVENT_ID,))
            result.status = "failed"
            result.message = "event cursor invalid"
            _finish_run(conn, result)
            raise

        for event in batch["items"]:
            item_type = event.get("item_type")
            change = event.get("type")
            item_id = event.get("item_id")
            if item_type == ITEM_TYPE_NOTE and item_id:
                if await _apply_note_event(conn, client, item_id, change, run_started):
                    result.notes_seen += 1
            elif item_type == ITEM_TYPE_FOLDER and item_id:
                if change == CHANGE_DELETE:
                    with repo.transaction():
                        repo.mark_folder_deleted(item_id)
                else:
                    needs_folder_refresh = True
            elif item_type == ITEM_TYPE_TAG and item_id:
                if change == CHANGE_DELETE:
                    with repo.transaction():
                        repo.mark_tag_deleted(item_id)
                else:
                    needs_tag_refresh = True
            elif item_type == ITEM_TYPE_RESOURCE and item_id:
                if change == CHANGE_DELETE:
                    with repo.transaction():
                        repo.mark_resource_deleted(item_id)
                else:
                    needs_resource_refresh = True

        if needs_folder_refresh:
            async for chunk in _chunked(
                client.get_folders(fields=FOLDER_FIELDS), cfg.joplin.page_limit
            ):
                with repo.transaction():
                    for folder in chunk:
                        repo.upsert_folder(folder, indexed_at=run_started)
                        result.folders_seen += 1
        if needs_tag_refresh:
            async for chunk in _chunked(client.get_tags(fields=TAG_FIELDS), cfg.joplin.page_limit):
                with repo.transaction():
                    for tag in chunk:
                        repo.upsert_tag(tag, indexed_at=run_started)
                        result.tags_seen += 1
        if needs_resource_refresh:
            async for chunk in _chunked(
                client.get_resources(fields=RESOURCE_FIELDS), cfg.joplin.page_limit
            ):
                with repo.transaction():
                    for resource in chunk:
                        repo.upsert_resource(resource, indexed_at=run_started)
            # A newly-known resource may now match existing note links.
            with repo.transaction():
                repo.rebuild_all_note_resources(indexed_at=run_started)

        # Advance cursor only after the full batch succeeded.
        with repo.transaction():
            new_cursor = batch.get("cursor")
            if new_cursor is not None:
                repo.set_meta(db.META_LAST_EVENT_ID, str(new_cursor))
            repo.set_meta(db.META_LAST_INCREMENTAL_INDEX_AT, str(started_at))

        log_event(logger, "index.incremental.completed", run_id=run_id, notes=result.notes_seen)
    except JoplinCursorInvalidError:
        raise
    except Exception as exc:
        result.status = "failed"
        result.errors += 1
        result.message = f"{type(exc).__name__}: {exc}"
        log_event(
            logger, "index.incremental.failed", level=40, run_id=run_id, error=type(exc).__name__
        )
        _finish_run(conn, result)
        raise
    _finish_run(conn, result)
    return result
