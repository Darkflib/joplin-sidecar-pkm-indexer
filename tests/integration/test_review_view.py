"""Tests for the 'needs review' view (PRD §4.6, heuristic defined in v0.2)."""

import sqlite3
import time

import pytest

from pkm_sidecar.config import AppConfig
from pkm_sidecar.models import ExtractedTask
from pkm_sidecar.repositories import NoteRepository

OLD = (int(time.time()) - 200 * 86400) * 1000  # ~200 days ago (stale)
NEW = int(time.time()) * 1000


@pytest.fixture
def repo(cfg: AppConfig, writer: sqlite3.Connection) -> NoteRepository:
    return NoteRepository(writer)


def _note(repo: NoteRepository, note_id: str, updated: int) -> None:
    repo.upsert_note(
        {"id": note_id, "title": note_id, "body": "b", "updated_time": updated}, indexed_at=1
    )


def test_review_heuristic(repo: NoteRepository) -> None:
    with repo.transaction():
        _note(repo, "stale_untagged", OLD)  # ✓ stale + untagged
        _note(repo, "stale_tagged_clean", OLD)  # ✗ stale but tagged, no open tasks
        _note(repo, "fresh_untagged", NEW)  # ✗ untagged but not stale
        _note(repo, "stale_tagged_open_task", OLD)  # ✓ stale + has an unchecked task
        repo.upsert_tag({"id": "t1", "title": "tag"}, indexed_at=1)
        repo.upsert_note_tag("stale_tagged_clean", "t1", indexed_at=1)
        repo.upsert_note_tag("stale_tagged_open_task", "t1", indexed_at=1)
        repo.replace_tasks_for_note(
            "stale_tagged_open_task",
            [
                ExtractedTask(
                    note_id="stale_tagged_open_task",
                    line_number=1,
                    checked=False,
                    text="x",
                    raw_line="- [ ] x",
                )
            ],
            indexed_at=1,
        )

    review = {n.id for n in repo.fetch_review(days=90)}
    assert review == {"stale_untagged", "stale_tagged_open_task"}
