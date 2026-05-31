"""Typed persistence layer — all SQL lives here (PRD §9, §13.2).

Reads return Pydantic models; writes accept plain dicts (straight from the Joplin
client) plus the extracted-task/link models. ``upsert_note`` returns whether the
body hash changed so the indexer can skip re-extracting tasks/links and leave the
FTS triggers to handle title/body reindexing.

Soft-deletes set ``deleted=1`` and are filtered out of every read (including FTS
search) via a JOIN on ``notes.deleted=0``. We do not surgically edit the
external-content ``notes_fts`` index — combined with the update triggers that
would corrupt its rowid bookkeeping — so filtering at query time is the safe path.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pkm_sidecar import db
from pkm_sidecar.models import (
    ExtractedLink,
    ExtractedTask,
    NoteRow,
    NoteSummary,
    ResourceRow,
    SearchHit,
    StatusCounts,
)

_SUMMARY_COLS = "id, parent_id, title, updated_time, user_updated_time, is_todo, todo_completed"


def build_fts_match_expression(user_query: str) -> str:
    """Public alias of :func:`db.fts_escape` (single source), exposed for tests."""
    return db.fts_escape(user_query)


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _summary(row: sqlite3.Row) -> NoteSummary:
    return NoteSummary(
        id=row["id"],
        parent_id=row["parent_id"],
        title=row["title"],
        updated_time=row["updated_time"],
        user_updated_time=row["user_updated_time"],
        is_todo=bool(row["is_todo"]),
        todo_completed=row["todo_completed"],
    )


class NoteRepository:
    """All queries over the index database, bound to one connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with db.transaction(self.conn) as conn:
            yield conn

    # --- upserts -----------------------------------------------------------

    def upsert_folder(self, folder: dict[str, Any], *, indexed_at: int) -> None:
        self.conn.execute(
            """
            INSERT INTO folders(id, parent_id, title, created_time, updated_time, indexed_at, deleted)
            VALUES (:id, :parent_id, :title, :created_time, :updated_time, :indexed_at, 0)
            ON CONFLICT(id) DO UPDATE SET
                parent_id=excluded.parent_id, title=excluded.title,
                created_time=excluded.created_time, updated_time=excluded.updated_time,
                indexed_at=excluded.indexed_at, deleted=0
            """,
            {
                "id": folder["id"],
                "parent_id": folder.get("parent_id") or None,
                "title": folder.get("title") or "",
                "created_time": folder.get("created_time"),
                "updated_time": folder.get("updated_time"),
                "indexed_at": indexed_at,
            },
        )

    def upsert_note(self, note: dict[str, Any], *, indexed_at: int) -> bool:
        body = note.get("body") or ""
        body_hash = _body_hash(body)
        existing = self.conn.execute(
            "SELECT body_hash FROM notes WHERE id = ?", (note["id"],)
        ).fetchone()
        body_changed = existing is None or existing["body_hash"] != body_hash
        self.conn.execute(
            """
            INSERT INTO notes(
                id, parent_id, title, body, body_hash, created_time, updated_time,
                user_created_time, user_updated_time, is_todo, todo_due, todo_completed,
                source_url, indexed_at, deleted)
            VALUES (
                :id, :parent_id, :title, :body, :body_hash, :created_time, :updated_time,
                :user_created_time, :user_updated_time, :is_todo, :todo_due, :todo_completed,
                :source_url, :indexed_at, 0)
            ON CONFLICT(id) DO UPDATE SET
                parent_id=excluded.parent_id, title=excluded.title, body=excluded.body,
                body_hash=excluded.body_hash, created_time=excluded.created_time,
                updated_time=excluded.updated_time, user_created_time=excluded.user_created_time,
                user_updated_time=excluded.user_updated_time, is_todo=excluded.is_todo,
                todo_due=excluded.todo_due, todo_completed=excluded.todo_completed,
                source_url=excluded.source_url, indexed_at=excluded.indexed_at, deleted=0
            """,
            {
                "id": note["id"],
                "parent_id": note.get("parent_id") or None,
                "title": note.get("title") or "",
                "body": body,
                "body_hash": body_hash,
                "created_time": note.get("created_time"),
                "updated_time": note.get("updated_time"),
                "user_created_time": note.get("user_created_time"),
                "user_updated_time": note.get("user_updated_time"),
                "is_todo": 1 if note.get("is_todo") else 0,
                "todo_due": note.get("todo_due"),
                "todo_completed": note.get("todo_completed"),
                "source_url": note.get("source_url"),
                "indexed_at": indexed_at,
            },
        )
        return body_changed

    def upsert_tag(self, tag: dict[str, Any], *, indexed_at: int) -> None:
        self.conn.execute(
            """
            INSERT INTO tags(id, title, created_time, updated_time, indexed_at, deleted)
            VALUES (:id, :title, :created_time, :updated_time, :indexed_at, 0)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, created_time=excluded.created_time,
                updated_time=excluded.updated_time, indexed_at=excluded.indexed_at, deleted=0
            """,
            {
                "id": tag["id"],
                "title": tag.get("title") or "",
                "created_time": tag.get("created_time"),
                "updated_time": tag.get("updated_time"),
                "indexed_at": indexed_at,
            },
        )

    def upsert_note_tag(self, note_id: str, tag_id: str, *, indexed_at: int) -> None:
        self.conn.execute(
            """
            INSERT INTO note_tags(note_id, tag_id, indexed_at) VALUES (?, ?, ?)
            ON CONFLICT(note_id, tag_id) DO UPDATE SET indexed_at=excluded.indexed_at
            """,
            (note_id, tag_id, indexed_at),
        )

    def replace_note_tags(
        self, note_id: str, tag_ids: list[str] | Iterator[str], *, indexed_at: int
    ) -> None:
        """Set a note's tag membership to exactly *tag_ids* (used by incremental sync)."""
        self.conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        self.conn.executemany(
            "INSERT INTO note_tags(note_id, tag_id, indexed_at) VALUES (?, ?, ?)",
            [(note_id, tag_id, indexed_at) for tag_id in tag_ids],
        )

    def upsert_resource(self, resource: dict[str, Any], *, indexed_at: int) -> None:
        self.conn.execute(
            """
            INSERT INTO resources(id, title, mime, filename, file_extension, size,
                created_time, updated_time, indexed_at, deleted)
            VALUES (:id, :title, :mime, :filename, :file_extension, :size,
                :created_time, :updated_time, :indexed_at, 0)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, mime=excluded.mime, filename=excluded.filename,
                file_extension=excluded.file_extension, size=excluded.size,
                created_time=excluded.created_time, updated_time=excluded.updated_time,
                indexed_at=excluded.indexed_at, deleted=0
            """,
            {
                "id": resource["id"],
                "title": resource.get("title"),
                "mime": resource.get("mime"),
                "filename": resource.get("filename"),
                "file_extension": resource.get("file_extension"),
                "size": resource.get("size"),
                "created_time": resource.get("created_time"),
                "updated_time": resource.get("updated_time"),
                "indexed_at": indexed_at,
            },
        )

    def rebuild_all_note_resources(self, *, indexed_at: int) -> None:
        """Derive note_resources from extracted internal links pointing at resources."""
        self.conn.execute("DELETE FROM note_resources")
        self.conn.execute(
            "INSERT OR IGNORE INTO note_resources(note_id, resource_id, indexed_at) "
            "SELECT DISTINCT l.note_id, substr(l.target, 3), ? "
            "FROM extracted_links l JOIN resources r ON r.id = substr(l.target, 3) "
            "WHERE l.link_type = 'internal_joplin'",
            (indexed_at,),
        )

    def rebuild_note_resources_for_note(self, note_id: str, *, indexed_at: int) -> None:
        self.conn.execute("DELETE FROM note_resources WHERE note_id = ?", (note_id,))
        self.conn.execute(
            "INSERT OR IGNORE INTO note_resources(note_id, resource_id, indexed_at) "
            "SELECT DISTINCT ?, substr(l.target, 3), ? FROM extracted_links l "
            "JOIN resources r ON r.id = substr(l.target, 3) "
            "WHERE l.note_id = ? AND l.link_type = 'internal_joplin'",
            (note_id, indexed_at, note_id),
        )

    def replace_tasks_for_note(
        self, note_id: str, tasks: Iterator[ExtractedTask] | list[ExtractedTask], *, indexed_at: int
    ) -> None:
        self.conn.execute("DELETE FROM extracted_tasks WHERE note_id = ?", (note_id,))
        self.conn.executemany(
            "INSERT INTO extracted_tasks(note_id, line_number, checked, text, raw_line, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (note_id, t.line_number, 1 if t.checked else 0, t.text, t.raw_line, indexed_at)
                for t in tasks
            ],
        )

    def replace_links_for_note(
        self, note_id: str, links: Iterator[ExtractedLink] | list[ExtractedLink], *, indexed_at: int
    ) -> None:
        self.conn.execute("DELETE FROM extracted_links WHERE note_id = ?", (note_id,))
        self.conn.executemany(
            "INSERT INTO extracted_links(note_id, line_number, link_text, target, link_type, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (note_id, lk.line_number, lk.link_text, lk.target, lk.link_type, indexed_at)
                for lk in links
            ],
        )

    # --- soft deletes ------------------------------------------------------

    def mark_note_deleted(self, note_id: str) -> None:
        self.conn.execute("UPDATE notes SET deleted = 1 WHERE id = ?", (note_id,))

    def mark_folder_deleted(self, folder_id: str) -> None:
        self.conn.execute("UPDATE folders SET deleted = 1 WHERE id = ?", (folder_id,))

    def mark_tag_deleted(self, tag_id: str) -> None:
        self.conn.execute("UPDATE tags SET deleted = 1 WHERE id = ?", (tag_id,))
        self.conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (tag_id,))

    def mark_resource_deleted(self, resource_id: str) -> None:
        self.conn.execute("UPDATE resources SET deleted = 1 WHERE id = ?", (resource_id,))

    def sweep_orphans(self, run_started_at: int) -> tuple[int, int, int, int]:
        """Mark rows untouched by a full rebuild as deleted.

        Returns ``(notes, folders, tags, resources)`` swept.
        """

        def sweep(table: str) -> int:
            return self.conn.execute(
                f"UPDATE {table} SET deleted = 1 WHERE deleted = 0 AND indexed_at < ?",
                (run_started_at,),
            ).rowcount

        return sweep("notes"), sweep("folders"), sweep("tags"), sweep("resources")

    # --- single-entity reads ----------------------------------------------

    def get_note(self, note_id: str) -> NoteRow | None:
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return None if row is None else NoteRow(**dict(row))

    def get_tasks_for_note(self, note_id: str) -> list[ExtractedTask]:
        rows = self.conn.execute(
            "SELECT id, note_id, line_number, checked, text, raw_line "
            "FROM extracted_tasks WHERE note_id = ? ORDER BY line_number",
            (note_id,),
        ).fetchall()
        return [
            ExtractedTask(
                id=r["id"],
                note_id=r["note_id"],
                line_number=r["line_number"],
                checked=bool(r["checked"]),
                text=r["text"],
                raw_line=r["raw_line"],
            )
            for r in rows
        ]

    def get_resources_for_note(self, note_id: str) -> list[ResourceRow]:
        rows = self.conn.execute(
            "SELECT r.id, r.title, r.mime, r.filename, r.file_extension, r.size, "
            "r.created_time, r.updated_time "
            "FROM note_resources nr JOIN resources r ON r.id = nr.resource_id "
            "WHERE nr.note_id = ? AND r.deleted = 0 ORDER BY r.title",
            (note_id,),
        ).fetchall()
        return [ResourceRow(**dict(r)) for r in rows]

    def get_links_for_note(self, note_id: str) -> list[ExtractedLink]:
        rows = self.conn.execute(
            "SELECT id, note_id, line_number, link_text, target, link_type "
            "FROM extracted_links WHERE note_id = ? ORDER BY id",
            (note_id,),
        ).fetchall()
        return [ExtractedLink(**dict(r)) for r in rows]

    # --- workflow views ----------------------------------------------------

    def fetch_recent(self, limit: int = 50) -> list[NoteSummary]:
        rows = self.conn.execute(
            f"SELECT {_SUMMARY_COLS} FROM notes WHERE deleted = 0 "
            "ORDER BY updated_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_summary(r) for r in rows]

    def fetch_inbox(self, folder_names: list[str], limit: int = 100) -> list[NoteSummary]:
        if not folder_names:
            return []
        placeholders = ", ".join("?" for _ in folder_names)
        rows = self.conn.execute(
            f"SELECT {', '.join('n.' + c for c in _SUMMARY_COLS.split(', '))} "
            "FROM notes n JOIN folders f ON f.id = n.parent_id "
            f"WHERE n.deleted = 0 AND f.deleted = 0 AND LOWER(f.title) IN ({placeholders}) "
            "ORDER BY n.updated_time DESC LIMIT ?",
            [*(name.lower() for name in folder_names), limit],
        ).fetchall()
        return [_summary(r) for r in rows]

    def fetch_untagged(self, limit: int = 100) -> list[NoteSummary]:
        rows = self.conn.execute(
            f"SELECT {_SUMMARY_COLS} FROM notes n WHERE n.deleted = 0 "
            "AND NOT EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id) "
            "ORDER BY n.updated_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_summary(r) for r in rows]

    def fetch_todos(self, limit: int = 100) -> list[NoteSummary]:
        rows = self.conn.execute(
            f"SELECT {_SUMMARY_COLS} FROM notes "
            "WHERE deleted = 0 AND is_todo = 1 AND todo_completed IS NULL "
            "UNION "
            f"SELECT {_SUMMARY_COLS} FROM notes n WHERE n.deleted = 0 "
            "AND EXISTS (SELECT 1 FROM extracted_tasks t WHERE t.note_id = n.id AND t.checked = 0) "
            "ORDER BY updated_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_summary(r) for r in rows]

    def fetch_stale(self, days: int = 90, limit: int = 100) -> list[NoteSummary]:
        cutoff_ms = (int(time.time()) - days * 86400) * 1000
        rows = self.conn.execute(
            f"SELECT {_SUMMARY_COLS} FROM notes WHERE deleted = 0 "
            "AND COALESCE(user_updated_time, updated_time) < ? "
            "ORDER BY COALESCE(user_updated_time, updated_time) ASC LIMIT ?",
            (cutoff_ms, limit),
        ).fetchall()
        return [_summary(r) for r in rows]

    def fts_search(self, query: str, limit: int = 50) -> list[SearchHit]:
        expr = build_fts_match_expression(query)
        if not expr:
            return []
        rows = self.conn.execute(
            "SELECT n.id, n.title, n.updated_time, n.parent_id, "
            "snippet(notes_fts, 1, '[', ']', '…', 12) AS snippet "
            "FROM notes_fts JOIN notes n ON n.rowid = notes_fts.rowid "
            "WHERE notes_fts MATCH ? AND n.deleted = 0 "
            "ORDER BY rank LIMIT ?",
            (expr, limit),
        ).fetchall()
        return [
            SearchHit(
                id=r["id"],
                title=r["title"],
                snippet=r["snippet"],
                updated_time=r["updated_time"],
                parent_id=r["parent_id"],
            )
            for r in rows
        ]

    # --- status & meta -----------------------------------------------------

    def get_status_counts(self) -> StatusCounts:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return StatusCounts(
            note_count=scalar("SELECT count(*) FROM notes WHERE deleted = 0"),
            folder_count=scalar("SELECT count(*) FROM folders WHERE deleted = 0"),
            tag_count=scalar("SELECT count(*) FROM tags WHERE deleted = 0"),
            task_count=scalar("SELECT count(*) FROM extracted_tasks"),
            resource_count=scalar("SELECT count(*) FROM resources WHERE deleted = 0"),
            deleted_count=scalar("SELECT count(*) FROM notes WHERE deleted = 1"),
        )

    def get_meta(self, key: str) -> str | None:
        return db.get_meta(self.conn, key)

    def set_meta(self, key: str, value: str) -> None:
        db.set_meta(self.conn, key, value)
