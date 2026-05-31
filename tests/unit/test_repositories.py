"""Tests for the NoteRepository persistence layer (PRD §9, §13.2, §18)."""

import sqlite3
import time
from pathlib import Path

import pytest

from pkm_sidecar import db
from pkm_sidecar.models import ExtractedLink, ExtractedTask
from pkm_sidecar.repositories import NoteRepository

NOW = 1_700_000_000_000  # arbitrary ms epoch for deterministic times


@pytest.fixture
def repo(tmp_path: Path) -> NoteRepository:
    path = tmp_path / "data" / "index.sqlite3"
    db.init_db(path)
    conn = db.open_writer_connection(path)
    return NoteRepository(conn)


def _note(repo: NoteRepository, **kw) -> bool:
    base = {"id": "n1", "title": "Title", "body": "body", "updated_time": NOW, "is_todo": 0}
    base.update(kw)
    return repo.upsert_note(base, indexed_at=base.get("updated_time") or 0)


class TestUpsertNote:
    def test_insert_then_get(self, repo: NoteRepository) -> None:
        _note(repo, id="n1", title="Hello", body="world")
        note = repo.get_note("n1")
        assert note is not None
        assert note.title == "Hello"
        assert note.body == "world"

    def test_body_changed_signal(self, repo: NoteRepository) -> None:
        assert _note(repo, id="n1", body="v1") is True  # new note
        assert _note(repo, id="n1", body="v1") is False  # same body
        assert _note(repo, id="n1", body="v2") is True  # changed body

    def test_body_hash_changes_with_body(self, repo: NoteRepository) -> None:
        _note(repo, id="n1", body="alpha")
        h1 = repo.get_note("n1").body_hash  # type: ignore[union-attr]
        _note(repo, id="n1", body="beta")
        h2 = repo.get_note("n1").body_hash  # type: ignore[union-attr]
        assert h1 != h2

    def test_reupsert_clears_deleted(self, repo: NoteRepository) -> None:
        _note(repo, id="n1")
        repo.mark_note_deleted("n1")
        assert repo.get_note("n1").deleted is True  # type: ignore[union-attr]
        _note(repo, id="n1")
        assert repo.get_note("n1").deleted is False  # type: ignore[union-attr]


class TestTasksAndLinks:
    def test_replace_tasks(self, repo: NoteRepository) -> None:
        _note(repo, id="n1")
        tasks = [
            ExtractedTask(note_id="n1", line_number=1, checked=False, text="a", raw_line="- [ ] a"),
            ExtractedTask(note_id="n1", line_number=2, checked=True, text="b", raw_line="- [x] b"),
        ]
        repo.replace_tasks_for_note("n1", tasks, indexed_at=NOW)
        got = repo.get_tasks_for_note("n1")
        assert len(got) == 2
        assert got[0].checked is False
        assert got[1].checked is True
        # replacing again wipes the old rows
        repo.replace_tasks_for_note("n1", [], indexed_at=NOW)
        assert repo.get_tasks_for_note("n1") == []

    def test_replace_links(self, repo: NoteRepository) -> None:
        _note(repo, id="n1")
        links = [
            ExtractedLink(note_id="n1", target=":/abc", link_type="internal_joplin", link_text="x"),
        ]
        repo.replace_links_for_note("n1", links, indexed_at=NOW)
        got = repo.get_links_for_note("n1")
        assert len(got) == 1
        assert got[0].link_type == "internal_joplin"


class TestViews:
    def test_fetch_recent_orders_desc(self, repo: NoteRepository) -> None:
        with repo.transaction():
            _note(repo, id="old", updated_time=NOW - 1000)
            _note(repo, id="new", updated_time=NOW)
        recent = repo.fetch_recent()
        assert [n.id for n in recent] == ["new", "old"]

    def test_recent_excludes_deleted(self, repo: NoteRepository) -> None:
        _note(repo, id="n1")
        repo.mark_note_deleted("n1")
        assert repo.fetch_recent() == []

    def test_fetch_untagged(self, repo: NoteRepository) -> None:
        with repo.transaction():
            _note(repo, id="tagged")
            _note(repo, id="untagged")
            repo.upsert_tag({"id": "t1", "title": "Tag"}, indexed_at=NOW)
            repo.upsert_note_tag("tagged", "t1", indexed_at=NOW)
        ids = {n.id for n in repo.fetch_untagged()}
        assert ids == {"untagged"}

    def test_fetch_inbox_case_insensitive(self, repo: NoteRepository) -> None:
        with repo.transaction():
            repo.upsert_folder({"id": "f1", "title": "Inbox"}, indexed_at=NOW)
            repo.upsert_folder({"id": "f2", "title": "Other"}, indexed_at=NOW)
            _note(repo, id="in", parent_id="f1")
            _note(repo, id="out", parent_id="f2")
        ids = {n.id for n in repo.fetch_inbox(["inbox"])}  # lowercase query matches "Inbox"
        assert ids == {"in"}

    def test_fetch_todos_joplin_and_markdown(self, repo: NoteRepository) -> None:
        with repo.transaction():
            _note(repo, id="jop", is_todo=1, todo_completed=None)
            _note(repo, id="done", is_todo=1, todo_completed=NOW)
            _note(repo, id="md")
            repo.replace_tasks_for_note(
                "md",
                [
                    ExtractedTask(
                        note_id="md", line_number=1, checked=False, text="x", raw_line="- [ ] x"
                    )
                ],
                indexed_at=NOW,
            )
            _note(repo, id="plain")
        ids = {n.id for n in repo.fetch_todos()}
        assert ids == {"jop", "md"}  # completed todo and plain note excluded

    def test_fetch_stale(self, repo: NoteRepository) -> None:
        recent_ms = int(time.time()) * 1000
        old_ms = (int(time.time()) - 200 * 86400) * 1000
        with repo.transaction():
            _note(repo, id="fresh", updated_time=recent_ms)
            _note(repo, id="stale", updated_time=old_ms)
        ids = {n.id for n in repo.fetch_stale(days=90)}
        assert ids == {"stale"}


class TestSearch:
    def test_search_returns_snippet(self, repo: NoteRepository) -> None:
        _note(repo, id="n1", title="RabbitMQ", body="how to configure RabbitMQ clustering")
        hits = repo.fts_search("clustering")
        assert len(hits) == 1
        assert hits[0].id == "n1"
        assert hits[0].snippet  # snippet present and non-empty

    def test_search_excludes_deleted(self, repo: NoteRepository) -> None:
        _note(repo, id="n1", body="findme")
        repo.mark_note_deleted("n1")
        assert repo.fts_search("findme") == []

    def test_empty_query_returns_empty(self, repo: NoteRepository) -> None:
        _note(repo, id="n1", body="anything")
        assert repo.fts_search("   ") == []


class TestStatusAndSweep:
    def test_status_counts(self, repo: NoteRepository) -> None:
        with repo.transaction():
            _note(repo, id="n1")
            _note(repo, id="n2")
            repo.upsert_folder({"id": "f1", "title": "F"}, indexed_at=NOW)
            repo.upsert_tag({"id": "t1", "title": "T"}, indexed_at=NOW)
            repo.mark_note_deleted("n2")
        counts = repo.get_status_counts()
        assert counts.note_count == 1
        assert counts.deleted_count == 1
        assert counts.folder_count == 1
        assert counts.tag_count == 1

    def test_sweep_orphans(self, repo: NoteRepository) -> None:
        run_started = NOW + 5000
        with repo.transaction():
            _note(repo, id="seen", updated_time=run_started + 1)  # indexed_at >= run_started
            _note(repo, id="orphan", updated_time=NOW)  # indexed_at < run_started
        notes, _, _, _ = repo.sweep_orphans(run_started)
        assert notes == 1
        assert repo.get_note("orphan").deleted is True  # type: ignore[union-attr]
        assert repo.get_note("seen").deleted is False  # type: ignore[union-attr]

    def test_meta_roundtrip(self, repo: NoteRepository) -> None:
        repo.set_meta("k", "v")
        assert repo.get_meta("k") == "v"


class TestMarkTagDeleted:
    def test_cascades_note_tags(self, repo: NoteRepository) -> None:
        with repo.transaction():
            _note(repo, id="n1")
            repo.upsert_tag({"id": "t1", "title": "T"}, indexed_at=NOW)
            repo.upsert_note_tag("n1", "t1", indexed_at=NOW)
        repo.mark_tag_deleted("t1")
        rows = repo.conn.execute("SELECT count(*) FROM note_tags WHERE tag_id='t1'").fetchone()[0]
        assert rows == 0
        # the note is now untagged
        assert {n.id for n in repo.fetch_untagged()} == {"n1"}


def test_repo_uses_row_factory(repo: NoteRepository) -> None:
    assert (
        isinstance(repo.conn.row_factory, type(sqlite3.Row)) or repo.conn.row_factory is sqlite3.Row
    )
