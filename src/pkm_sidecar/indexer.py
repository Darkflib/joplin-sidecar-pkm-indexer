"""Indexing lifecycle: the writer connection, serialisation lock, and background
loop that drives incremental sync (PRD §11).

:class:`IndexerHandle` owns the single writer connection and an ``asyncio.Lock``
that serialises full rebuild vs incremental sync. The FastAPI lifespan creates
one handle, optionally starts the background loop, and stops it on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3

from pkm_sidecar import services
from pkm_sidecar.config import AppConfig
from pkm_sidecar.errors import IndexInProgressError, JoplinCursorInvalidError
from pkm_sidecar.joplin_client import JoplinClient
from pkm_sidecar.logging_config import LogOnceGuard, get_logger, log_event
from pkm_sidecar.services import IndexRunResult

logger = get_logger("indexer")

_STOP_DEADLINE_SECONDS = 3.0
# How long to stay quiet after logging a failed tick. The loop retries every
# ``event_poll_seconds`` (10s by default), so an overnight Joplin outage would
# otherwise emit thousands of identical tracebacks (PRD §17.2).
_TICK_FAILURE_COOLDOWN_SECONDS = 300.0


class IndexerHandle:
    """Owns the writer connection, the rebuild/sync lock, and the background task."""

    def __init__(self, cfg: AppConfig, conn: sqlite3.Connection, client: JoplinClient) -> None:
        self.cfg = cfg
        self.conn = conn
        self.client = client
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[object]] = set()
        self._rebuild_in_progress = False
        self._last_result: IndexRunResult | None = None
        self._last_error: str | None = None
        self._tick_failure_guard = LogOnceGuard()
        self._consecutive_tick_failures = 0

    # --- introspection -----------------------------------------------------

    @property
    def rebuild_in_progress(self) -> bool:
        return self._rebuild_in_progress

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def status(self) -> dict[str, object]:
        return {
            "rebuild_in_progress": self._rebuild_in_progress,
            "last_error": self._last_error,
            "last_run": None if self._last_result is None else vars(self._last_result),
        }

    # --- entry points ------------------------------------------------------

    async def _do_rebuild(self, run_id: int | None = None) -> IndexRunResult:
        async with self._lock:
            self._rebuild_in_progress = True
            try:
                result = await services.full_rebuild(
                    self.conn, self.client, self.cfg, run_id=run_id
                )
                self._last_result = result
                self._last_error = result.message if result.status != "success" else None
                return result
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self._rebuild_in_progress = False

    async def run_full_rebuild(self) -> IndexRunResult:
        if self._rebuild_in_progress:
            raise IndexInProgressError("A full rebuild is already running.")
        return await self._do_rebuild()

    def dispatch_rebuild(self) -> tuple[int, int]:
        """Start a rebuild in the background; return (run_id, started_at) for a 202.

        The index_runs row is created synchronously so the caller gets a run_id,
        and ``rebuild_in_progress`` flips immediately so a second request 409s.
        """
        if self._rebuild_in_progress:
            raise IndexInProgressError("A full rebuild is already running.")
        run_id, started_at = services.start_run_record(self.conn, "full")
        self._rebuild_in_progress = True
        self._track(asyncio.create_task(self._do_rebuild(run_id)))
        return run_id, started_at

    def dispatch_sync(self) -> None:
        """Fire-and-forget an incremental sync pass."""
        self._track(asyncio.create_task(self.run_incremental_once()))

    async def reindex_one(self, note_id: str) -> bool:
        """Re-index a single note under the lock; returns False if Joplin lacks it."""
        async with self._lock:
            return await services.reindex_note(self.conn, self.client, note_id)

    def _track(self, task: asyncio.Task[object]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error("Dispatched index task failed: %s", type(exc).__name__)

    async def run_incremental_once(self) -> IndexRunResult:
        try:
            async with self._lock:
                result = await services.incremental_sync_once(self.conn, self.client, self.cfg)
            self._last_result = result
            self._last_error = result.message if result.status != "success" else None
            return result
        except JoplinCursorInvalidError:
            # Lock released above; rebuild re-acquires it cleanly (PRD §17.4).
            log_event(logger, "event.cursor_invalid", level=30)
            result = await self.run_full_rebuild()
            result.message = "cursor invalid → rebuilt"
            self._last_result = result
            return result

    # --- background loop ---------------------------------------------------

    async def start(self, *, enable_background: bool = True) -> None:
        if enable_background and self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=_STOP_DEADLINE_SECONDS)
            except TimeoutError:
                self._task.cancel()
            finally:
                self._task = None
        # Cancel any in-flight dispatched rebuild/sync tasks.
        for task in list(self._tasks):
            task.cancel()

    def _log_tick_failure(self, exc: Exception) -> None:
        """Log a failed tick with its traceback, at most once per cooldown.

        The first failure is logged in full; subsequent ones stay quiet until the
        cooldown expires and then report how many ticks have failed in a row, so
        a sustained outage is still visible without flooding the log. Either way
        ``/api/status`` keeps reporting ``last_error`` throughout.
        """
        self._consecutive_tick_failures += 1
        if not self._tick_failure_guard.should_log(
            f"index.tick_failed:{type(exc).__name__}",
            cooldown_seconds=_TICK_FAILURE_COOLDOWN_SECONDS,
        ):
            return
        if self._consecutive_tick_failures == 1:
            logger.error("Incremental sync tick failed", exc_info=exc)
        else:
            logger.error(
                "Incremental sync tick still failing (%d consecutive): %s",
                self._consecutive_tick_failures,
                type(exc).__name__,
            )

    async def _run_loop(self) -> None:
        log_event(logger, "service.start", component="indexer")
        while not self._stop.is_set():
            try:
                await self.run_incremental_once()
                # Recovered: let the next failure log immediately rather than
                # being swallowed by a cooldown left over from an earlier outage.
                self._consecutive_tick_failures = 0
                self._tick_failure_guard.reset()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log_tick_failure(exc)
            # interval elapsed (TimeoutError) -> run the next tick; stop_event -> exit
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.joplin.event_poll_seconds
                )
        log_event(logger, "service.stop", component="indexer")


def create_indexer(cfg: AppConfig, conn: sqlite3.Connection, client: JoplinClient) -> IndexerHandle:
    """Build an :class:`IndexerHandle`; call ``await handle.start(...)`` to run the loop."""
    return IndexerHandle(cfg, conn, client)
