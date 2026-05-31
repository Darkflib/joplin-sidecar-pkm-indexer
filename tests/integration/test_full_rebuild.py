"""Integration test: full rebuild against a mocked Joplin (PRD §18.3, §18.10)."""

import sqlite3

from pkm_sidecar import services
from pkm_sidecar.config import AppConfig
from pkm_sidecar.repositories import NoteRepository
from tests._fake_joplin import FakeJoplin


async def test_full_rebuild_indexes_all_entities(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    fake.add_folder("f1", "Inbox")
    fake.add_note(
        "n1", "First", "- [ ] a task\n[ref](:/0123456789abcdef0123456789abcdef)", parent_id="f1"
    )
    fake.add_note("n2", "Second", "plain body about RabbitMQ")
    fake.add_tag("t1", "project", note_ids=("n1",))

    async with fake.client() as client:
        result = await services.full_rebuild(writer, client, cfg)

    assert result.status == "success"
    assert result.notes_seen == 2
    assert result.folders_seen == 1
    assert result.tags_seen == 1

    repo = NoteRepository(writer)
    assert {n.id for n in repo.fetch_recent()} == {"n1", "n2"}
    assert repo.get_status_counts().note_count == 2
    # tasks + links extracted for n1
    assert len(repo.get_tasks_for_note("n1")) == 1
    assert repo.get_links_for_note("n1")[0].link_type == "internal_joplin"
    # note_tags built from get_tag_notes: n1 tagged, n2 untagged
    assert {n.id for n in repo.fetch_untagged()} == {"n2"}
    # FTS works
    assert [h.id for h in repo.fts_search("RabbitMQ")] == ["n2"]


async def test_rebuild_sweeps_orphans(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "Keep", "x")
    fake.add_note("n2", "Remove", "y")
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
        # n2 disappears from Joplin before the next rebuild
        del fake.notes["n2"]
        await services.full_rebuild(writer, client, cfg)

    repo = NoteRepository(writer)
    assert {n.id for n in repo.fetch_recent()} == {"n1"}
    assert repo.get_note("n2").deleted is True  # type: ignore[union-attr]


async def test_rebuild_issues_only_get_requests(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    fake.add_note("n1", "T", "b")
    fake.add_tag("t1", "tag", note_ids=("n1",))
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)
    assert fake.only_get_requests()  # PRD §18.10 no-mutation invariant
