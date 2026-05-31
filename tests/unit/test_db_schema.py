"""Tests for the SQLite schema and db driver (PRD §9, §17.3)."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from pkm_sidecar import db
from pkm_sidecar.errors import DatabaseLockedError, SchemaVersionMismatchError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "index.sqlite3"
    db.init_db(path)
    return path


def _insert_note(conn: sqlite3.Connection, note_id: str, title: str, body: str) -> None:
    conn.execute(
        "INSERT INTO notes(id, title, body, body_hash, is_todo, indexed_at) "
        "VALUES (?, ?, ?, ?, 0, 0)",
        (note_id, title, body, "hash"),
    )


class TestInit:
    def test_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "index.sqlite3"
        db.init_db(path)
        db.init_db(path)  # second call must not raise
        assert path.exists()

    def test_creates_all_tables(self, db_path: Path) -> None:
        conn = db.open_reader_connection(db_path)
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        assert {
            "meta",
            "folders",
            "notes",
            "tags",
            "note_tags",
            "extracted_tasks",
            "extracted_links",
            "index_runs",
            "resources",
            "note_resources",
        } <= names

    def test_schema_version_written(self, db_path: Path) -> None:
        conn = db.open_reader_connection(db_path)
        assert db.get_meta(conn, db.SCHEMA_VERSION_KEY) == str(db.SCHEMA_VERSION)
        conn.close()

    def test_schema_version_mismatch_raises(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        db.set_meta(conn, db.SCHEMA_VERSION_KEY, "999")
        conn.close()
        with pytest.raises(SchemaVersionMismatchError):
            db.init_db(db_path)


class TestPragmas:
    def test_pragmas_active(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()

    def test_reader_is_query_only(self, db_path: Path) -> None:
        conn = db.open_reader_connection(db_path)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
        conn.close()

    def test_fts_available(self, db_path: Path) -> None:
        # Probed on a writable connection (query_only readers can't create the temp probe).
        conn = db.open_writer_connection(db_path)
        assert db.fts_available(conn) is True
        conn.close()


class TestReferentialTolerance:
    def test_note_with_unknown_parent_is_accepted(self, db_path: Path) -> None:
        # Joplin can hand us a note whose parent folder isn't in /folders; the
        # derived index must record it rather than fail the insert (no parent FK).
        conn = db.open_writer_connection(db_path)
        conn.execute(
            "INSERT INTO notes(id, parent_id, title, body, body_hash, is_todo, indexed_at) "
            "VALUES ('n1', 'missing-folder', 'N', 'b', 'h', 0, 0)"
        )
        row = conn.execute("SELECT parent_id FROM notes WHERE id='n1'").fetchone()
        assert row["parent_id"] == "missing-folder"
        conn.close()

    def test_note_tag_with_unknown_note_is_accepted(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        conn.execute("INSERT INTO tags(id, title, indexed_at) VALUES ('t1', 'T', 0)")
        # note_id 'ghost' is not in notes — must not raise (no FK on note_tags).
        conn.execute("INSERT INTO note_tags(note_id, tag_id, indexed_at) VALUES ('ghost', 't1', 0)")
        assert conn.execute("SELECT count(*) FROM note_tags").fetchone()[0] == 1
        conn.close()


class TestFtsTriggers:
    def test_insert_and_search(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        _insert_note(conn, "n1", "RabbitMQ guide", "how to configure RabbitMQ")
        hits = conn.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'RabbitMQ'"
        ).fetchall()
        assert len(hits) == 1
        conn.close()

    def test_update_reindexes(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        _insert_note(conn, "n1", "Title", "kafka content")
        conn.execute("UPDATE notes SET body='rabbitmq content' WHERE id='n1'")
        assert (
            conn.execute("SELECT count(*) FROM notes_fts WHERE notes_fts MATCH 'kafka'").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM notes_fts WHERE notes_fts MATCH 'rabbitmq'"
            ).fetchone()[0]
            == 1
        )
        conn.close()

    def test_delete_removes_from_fts(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        _insert_note(conn, "n1", "Title", "findme content")
        conn.execute("DELETE FROM notes WHERE id='n1'")
        assert (
            conn.execute(
                "SELECT count(*) FROM notes_fts WHERE notes_fts MATCH 'findme'"
            ).fetchone()[0]
            == 0
        )
        conn.close()


class TestFtsEscape:
    def test_quotes_tokens(self) -> None:
        assert db.fts_escape("rabbit mq") == '"rabbit" "mq"'

    def test_escapes_embedded_quote(self) -> None:
        assert db.fts_escape('a"b') == '"a""b"'

    def test_empty_returns_empty(self) -> None:
        assert db.fts_escape("   ") == ""

    def test_operators_are_neutralised(self, db_path: Path) -> None:
        # A raw FTS operator would be a syntax error; escaped it is a literal phrase.
        conn = db.open_writer_connection(db_path)
        _insert_note(conn, "n1", "Title", "alpha content")
        expr = db.fts_escape("alpha OR")
        rows = conn.execute(
            "SELECT count(*) FROM notes_fts WHERE notes_fts MATCH ?", (expr,)
        ).fetchone()[0]
        assert rows == 0  # no note literally contains the token "OR"
        conn.close()


class TestTransaction:
    def test_commit(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        with db.transaction(conn):
            db.set_meta(conn, "k", "v")
        assert db.get_meta(conn, "k") == "v"
        conn.close()

    def test_rollback_on_exception(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        with pytest.raises(ValueError), db.transaction(conn):
            db.set_meta(conn, "k", "v")
            raise ValueError("boom")
        assert db.get_meta(conn, "k") is None
        conn.close()

    def test_lock_retry_succeeds_when_competitor_releases(self, db_path: Path) -> None:
        holder = db.open_writer_connection(db_path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO meta(key, value) VALUES ('held', '1')")

        released = threading.Event()

        def release_soon() -> None:
            time.sleep(0.1)
            holder.execute("COMMIT")
            released.set()

        t = threading.Thread(target=release_soon)
        t.start()

        writer = db.open_writer_connection(db_path)
        writer.execute("PRAGMA busy_timeout=0")  # force our retry loop, not SQLite's wait
        with db.transaction(writer):  # should retry until holder commits
            db.set_meta(writer, "second", "2")
        t.join()
        assert released.is_set()
        assert db.get_meta(writer, "second") == "2"
        writer.close()
        holder.close()

    def test_lock_raises_when_never_released(self, db_path: Path) -> None:
        holder = db.open_writer_connection(db_path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO meta(key, value) VALUES ('held', '1')")

        writer = db.open_writer_connection(db_path)
        writer.execute("PRAGMA busy_timeout=0")  # fail fast so the retry budget exhausts quickly
        with pytest.raises(DatabaseLockedError), db.transaction(writer):
            db.set_meta(writer, "x", "y")
        holder.execute("ROLLBACK")
        writer.close()
        holder.close()


class TestResetDerived:
    def test_preserves_notes_clears_derived(self, db_path: Path) -> None:
        conn = db.open_writer_connection(db_path)
        _insert_note(conn, "n1", "Title", "body")
        conn.execute(
            "INSERT INTO extracted_tasks(note_id, line_number, checked, text, raw_line, indexed_at) "
            "VALUES ('n1', 1, 0, 't', '- [ ] t', 0)"
        )
        db.set_meta(conn, db.META_LAST_EVENT_ID, "cursor-123")
        db.reset_derived(conn)
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM extracted_tasks").fetchone()[0] == 0
        assert db.get_meta(conn, db.META_LAST_EVENT_ID) is None
        conn.close()
