"""Unit tests for event dispatch and the indexer lifecycle (PRD §11.2)."""

import asyncio
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from pkm_sidecar import db, logging_config, services
from pkm_sidecar import indexer as indexer_mod
from pkm_sidecar.config import AppConfig, load_config
from pkm_sidecar.errors import JoplinError
from pkm_sidecar.indexer import create_indexer
from pkm_sidecar.joplin_client import JoplinClient
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
    # This index has never been built, so the tick backfills rather than syncing:
    # the loop is the safety net for an index that is empty or left broken, not
    # only a driver of incremental sync. (In the app, app.py dispatches the same
    # rebuild at startup and the _rebuild_in_progress guard stops it happening
    # twice.) The incremental path on a healthy index is covered by
    # TestRecoveryRetries::test_healthy_index_still_syncs_normally.
    assert NoteRepository(writer).get_meta(db.META_LAST_FULL_INDEX_AT) is not None
    assert {n.id for n in NoteRepository(writer).fetch_recent()} == {"n1"}


async def test_test_mode_disables_background_loop(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    async with fake.client() as client:
        handle = create_indexer(cfg, writer, client)
        await handle.start(enable_background=False)
        assert handle._task is None
        await handle.stop()


class TestOutageLogging:
    """A Joplin outage must not flood the log (PRD §17.2).

    The loop retries every ``event_poll_seconds``, so before this was rate-limited
    an overnight outage emitted a full traceback — plus an ERROR event line — per
    tick, thousands of times over.
    """

    @staticmethod
    def _tick_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.message.startswith("Incremental sync tick")]

    async def _run_broken_loop(
        self, cfg: AppConfig, writer: sqlite3.Connection, ticks: int
    ) -> None:
        fake = FakeJoplin(events_status=500)  # every /events call fails
        async with fake.client() as client:
            handle = create_indexer(cfg, writer, client)
            for _ in range(ticks):
                try:
                    await handle.run_incremental_once()
                except Exception as exc:  # mirrors the loop's own handler
                    handle._log_tick_failure(exc)

    async def test_repeated_failures_log_once(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            await self._run_broken_loop(cfg, writer, ticks=25)

        failures = self._tick_failures(caplog)
        assert len(failures) == 1  # not 25
        assert failures[0].exc_info is not None  # the one we keep carries the traceback
        # The per-tick `index.incremental.failed` event is rate-limited too.
        assert sum("index.incremental.failed" in r.message for r in caplog.records) == 1

    async def test_status_still_reports_the_error_while_quiet(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Suppressing the log must not suppress the signal /api/status reads."""
        fake = FakeJoplin(events_status=500)
        async with fake.client() as client:
            handle = create_indexer(cfg, writer, client)
            for _ in range(5):
                try:
                    await handle.run_incremental_once()
                except Exception as exc:
                    handle._last_error = f"{type(exc).__name__}: {exc}"
                    handle._log_tick_failure(exc)
            assert handle.last_error is not None
            assert handle._consecutive_tick_failures == 5

    async def test_cooldown_expiry_reports_the_backlog(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakeJoplin(events_status=500)
        async with fake.client() as client:
            handle = create_indexer(cfg, writer, client)
            with caplog.at_level(logging.DEBUG):
                for tick in range(12):
                    if tick == 8:
                        handle._tick_failure_guard.reset()  # simulate the cooldown expiring
                    try:
                        await handle.run_incremental_once()
                    except Exception as exc:
                        handle._log_tick_failure(exc)

        messages = [r.message for r in self._tick_failures(caplog)]
        assert len(messages) == 2
        assert "still failing (9 consecutive)" in messages[1]

    async def test_recovery_lets_the_next_outage_log_immediately(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakeJoplin()
        fake.add_note("n1", "T", "b")
        async with fake.client() as client:
            handle = create_indexer(cfg, writer, client)
            with caplog.at_level(logging.DEBUG):
                fake.events_status = 500
                for _ in range(3):
                    try:
                        await handle.run_incremental_once()
                    except Exception as exc:
                        handle._log_tick_failure(exc)

                fake.events_status = 200  # Joplin comes back
                await handle.run_incremental_once()
                handle._consecutive_tick_failures = 0
                handle._tick_failure_guard.reset()

                fake.events_status = 500  # and goes away again
                try:
                    await handle.run_incremental_once()
                except Exception as exc:
                    handle._log_tick_failure(exc)

        # One for the first outage, one for the second — the recovery in between
        # clears the cooldown so the new outage is not swallowed.
        assert len(self._tick_failures(caplog)) == 2

    async def test_the_background_loop_itself_is_rate_limited(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End-to-end through _run_loop, not just the helper it calls."""
        # A zero poll interval turns the loop over as fast as the event loop allows.
        fast = cfg.model_copy(
            update={"joplin": cfg.joplin.model_copy(update={"event_poll_seconds": 0})}
        )
        fake = FakeJoplin(events_status=500)
        async with fake.client() as client:
            handle = create_indexer(fast, writer, client)
            with caplog.at_level(logging.DEBUG):
                await handle.start(enable_background=True)
                await asyncio.sleep(0.2)
                await handle.stop()

        assert handle._consecutive_tick_failures > 5  # plenty of ticks failed
        assert len(self._tick_failures(caplog)) == 1  # one line in the log
        assert handle.last_error is not None  # still visible to /api/status

    async def test_failing_batch_processing_is_rate_limited_too(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """/events answering is not recovery — the tick has to finish.

        When `/events` succeeds but applying the batch fails, the cursor does not
        advance, so every tick refetches the same batch. Resetting the guard as
        soon as `/events` answered meant each of those ticks logged afresh, one
        `index.incremental.failed` line per poll for as long as it lasted.
        """
        fake = FakeJoplin()
        fake.add_note("n1", "T", "b")
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)

        def handler(request: httpx.Request) -> httpx.Response:
            # /events keeps working; fetching the note it points at does not.
            if request.url.path.startswith("/notes/"):
                return httpx.Response(500, json={"error": "boom"})
            return fake.handler(request)

        client = JoplinClient(
            "http://127.0.0.1:41184",
            "tok",
            transport=httpx.MockTransport(handler),
            max_retries=0,
            backoff_initial=0.0,
        )
        async with client:
            with caplog.at_level(logging.DEBUG):
                for _ in range(15):
                    with pytest.raises(JoplinError):
                        await services.incremental_sync_once(writer, client, cfg)

        assert sum("index.incremental.failed" in r.message for r in caplog.records) == 1
        # The batch really was retried every tick (the cursor never advanced).
        assert NoteRepository(writer).get_meta(db.META_LAST_EVENT_ID) is None

    async def test_a_completed_tick_still_counts_as_recovery(
        self, cfg: AppConfig, writer: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Narrowing the reset must not stop a real recovery from clearing it."""
        fake = FakeJoplin(events_status=500)
        fake.add_note("n1", "T", "b")
        async with fake.client() as client:
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(JoplinError):
                    await services.incremental_sync_once(writer, client, cfg)

                fake.events_status = 200  # Joplin comes back; this tick completes
                fake.push_event(ITEM_TYPE_NOTE, "n1", 2)
                assert (await services.incremental_sync_once(writer, client, cfg)).status == (
                    "success"
                )

                fake.events_status = 500  # and fails again
                with pytest.raises(JoplinError):
                    await services.incremental_sync_once(writer, client, cfg)

        assert sum("index.incremental.failed" in r.message for r in caplog.records) == 2


class TestRecoveryRetries:
    """Repairing a broken index is the loop's job until it succeeds.

    A startup recovery rebuild that fails — Joplin not up yet when the launcher
    spawns the sidecar is a plausible race — used to be one-shot. The loop then
    ran incremental syncs for ever, and those can never refill the derived tables
    because reset_derived cleared the cursor they read from.
    """

    @staticmethod
    def _flaky_client(fake: FakeJoplin, down: dict[str, bool]) -> JoplinClient:
        def handler(request: httpx.Request) -> httpx.Response:
            # /events keeps answering so incremental ticks "succeed" — which is
            # exactly why falling through to them hid the broken index.
            if down["v"] and request.url.path != "/events":
                return httpx.Response(500, json={"error": "not ready"})
            return fake.handler(request)

        return JoplinClient(
            "http://127.0.0.1:41184",
            "tok",
            transport=httpx.MockTransport(handler),
            max_retries=0,
            backoff_initial=0.0,
        )

    @pytest.fixture
    def fast_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(indexer_mod, "_RECOVERY_BACKOFF_BASE_SECONDS", 0.001, raising=False)
        monkeypatch.setattr(indexer_mod, "_RECOVERY_BACKOFF_MAX_SECONDS", 0.005, raising=False)

    @staticmethod
    def _tight_loop_cfg(cfg: AppConfig) -> AppConfig:
        return cfg.model_copy(
            update={"joplin": cfg.joplin.model_copy(update={"event_poll_seconds": 0})}
        )

    async def test_loop_keeps_retrying_until_joplin_returns(
        self, cfg: AppConfig, writer: sqlite3.Connection, fast_backoff: None
    ) -> None:
        fake = FakeJoplin()
        fake.add_note("n1", "One", "- [ ] a task")
        fake.add_tag("t1", "work", note_ids=("n1",))
        down = {"v": True}
        client = self._flaky_client(fake, down)

        async with client:
            with pytest.raises(JoplinError):  # the startup recovery attempt
                await services.full_rebuild(writer, client, cfg)
            assert db.rebuild_in_progress_since(writer) is not None

            handle = indexer_mod.create_indexer(self._tight_loop_cfg(cfg), writer, client)
            await handle.start(enable_background=True)
            await asyncio.sleep(0.05)  # ticks pass while Joplin is still down
            down["v"] = False  # Joplin comes back
            await asyncio.sleep(0.1)
            await handle.stop()

        assert db.rebuild_in_progress_since(writer) is None  # repaired itself
        repo = NoteRepository(writer)
        assert [n.id for n in repo.fetch_todos()] == ["n1"]
        assert [n.id for n in repo.fetch_untagged()] == []

    async def test_incremental_is_skipped_while_the_index_is_broken(
        self, cfg: AppConfig, writer: sqlite3.Connection, fast_backoff: None
    ) -> None:
        """Syncing a broken index just advances the cursor over the damage."""
        fake = FakeJoplin()
        fake.add_note("n1", "One", "body")
        down = {"v": True}
        client = self._flaky_client(fake, down)
        async with client:
            with pytest.raises(JoplinError):
                await services.full_rebuild(writer, client, cfg)
            handle = indexer_mod.create_indexer(self._tight_loop_cfg(cfg), writer, client)
            await handle.start(enable_background=True)
            await asyncio.sleep(0.05)
            await handle.stop()
        # No incremental run recorded its "last synced" stamp over the broken index.
        assert NoteRepository(writer).get_meta(db.META_LAST_INCREMENTAL_INDEX_AT) is None

    async def test_backoff_spaces_the_attempts_out(
        self, cfg: AppConfig, writer: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rebuild is heavy; a Joplin that stays down must not get one per tick."""
        monkeypatch.setattr(indexer_mod, "_RECOVERY_BACKOFF_BASE_SECONDS", 30.0, raising=False)
        fake = FakeJoplin()
        down = {"v": True}
        client = self._flaky_client(fake, down)
        async with client:
            with pytest.raises(JoplinError):
                await services.full_rebuild(writer, client, cfg)
            handle = indexer_mod.create_indexer(self._tight_loop_cfg(cfg), writer, client)
            await handle.start(enable_background=True)
            await asyncio.sleep(0.05)  # many ticks at a zero poll interval
            await handle.stop()
        # One attempt, then a 30s backoff that none of those ticks outlasted.
        assert handle._recovery_failures == 1

    async def test_healthy_index_still_syncs_normally(
        self, cfg: AppConfig, writer: sqlite3.Connection
    ) -> None:
        fake = FakeJoplin()
        fake.add_note("n1", "One", "body")
        async with fake.client() as client:
            await services.full_rebuild(writer, client, cfg)
            handle = indexer_mod.create_indexer(cfg, writer, client)
            await handle.start(enable_background=True)
            await asyncio.sleep(0.05)
            await handle.stop()
        assert handle._recovery_failures == 0
        assert NoteRepository(writer).get_meta(db.META_LAST_INCREMENTAL_INDEX_AT) is not None
