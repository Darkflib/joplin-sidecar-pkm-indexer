"""Integration test: full rebuild against a mocked Joplin (PRD §18.3, §18.10)."""

import sqlite3

import httpx
import pytest

from pkm_sidecar import db, services
from pkm_sidecar.config import AppConfig
from pkm_sidecar.errors import JoplinError
from pkm_sidecar.joplin_client import JoplinClient
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


async def test_rebuild_indexes_resources_and_links_them(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    """v0.2: resources are indexed and note_resources derived from embedded links."""
    rid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    fake = FakeJoplin()
    fake.add_note("n1", "Has attachment", f"see ![diagram](:/{rid})")
    fake.add_note("n2", "No attachment", "plain")
    fake.add_resource(rid, title="diagram.png", mime="image/png", size=1234)

    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)

    repo = NoteRepository(writer)
    assert repo.get_status_counts().resource_count == 1
    res = repo.get_resources_for_note("n1")
    assert [r.id for r in res] == [rid]
    assert res[0].mime == "image/png"
    assert repo.get_resources_for_note("n2") == []


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


class TestIncompleteIndexFlag:
    """The flag brackets the window where the derived tables are known-partial."""

    async def test_successful_rebuild_leaves_no_flag(
        self, cfg: AppConfig, writer: sqlite3.Connection
    ) -> None:
        fake = FakeJoplin()
        fake.add_note("n1", "One", "- [ ] task")
        async with fake.client() as client:
            await services.full_rebuild(writer, client, cfg)
        assert db.rebuild_in_progress_since(writer) is None
        assert services.rebuild_reason(cfg, writer) is None

    async def test_rebuild_that_fails_partway_leaves_the_flag_up(
        self, cfg: AppConfig, writer: sqlite3.Connection
    ) -> None:
        """Joplin going away mid-rebuild wipes the derived tables just the same."""
        fake = FakeJoplin()
        fake.add_note("n1", "One", "- [ ] task")
        fake.add_tag("t1", "work", note_ids=("n1",))
        async with fake.client() as client:
            await services.full_rebuild(writer, client, cfg)  # a good run first
            assert writer.execute("SELECT count(*) FROM extracted_tasks").fetchone()[0] == 1

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/notes":  # dies after reset_derived
                    return httpx.Response(500, json={"error": "boom"})
                return fake.handler(request)

            broken = JoplinClient(
                "http://127.0.0.1:41184",
                "tok",
                transport=httpx.MockTransport(handler),
                max_retries=0,
                backoff_initial=0.0,
            )
            async with broken:
                with pytest.raises(JoplinError):
                    await services.full_rebuild(writer, broken, cfg)

        # Derived data really is gone, and the old completion stamp still stands…
        assert writer.execute("SELECT count(*) FROM extracted_tasks").fetchone()[0] == 0
        assert writer.execute("SELECT count(*) FROM note_tags").fetchone()[0] == 0
        assert NoteRepository(writer).get_meta(db.META_LAST_FULL_INDEX_AT) is not None
        # …so the flag is the only thing that knows, and it does.
        assert services.rebuild_reason(cfg, writer) == "interrupted_rebuild"

    async def test_the_next_rebuild_repairs_the_index_and_clears_the_flag(
        self, cfg: AppConfig, writer: sqlite3.Connection
    ) -> None:
        """End to end: wipe, interrupt, restart, and the views are whole again."""
        fake = FakeJoplin()
        fake.add_note("n1", "One", "- [ ] a task")
        fake.add_tag("t1", "work", note_ids=("n1",))
        repo = NoteRepository(writer)

        async with fake.client() as client:
            await services.full_rebuild(writer, client, cfg)
            db.reset_derived(writer, started_at=1700000123)  # killed mid-rebuild

            # What a restart would see before it acts.
            assert [n.id for n in repo.fetch_untagged()] == ["n1"]  # wrong: n1 is tagged
            assert [n.id for n in repo.fetch_todos()] == []  # wrong: n1 has an open task
            assert services.rebuild_reason(cfg, writer) == "interrupted_rebuild"

            await services.full_rebuild(writer, client, cfg)  # what startup dispatches

        assert [n.id for n in repo.fetch_untagged()] == []
        assert [n.id for n in repo.fetch_todos()] == ["n1"]
        assert services.rebuild_reason(cfg, writer) is None
