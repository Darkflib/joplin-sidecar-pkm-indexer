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
from pkm_sidecar.logging_config import get_logger, log_event
from pkm_sidecar.services import IndexRunResult

logger = get_logger("indexer")

_STOP_DEADLINE_SECONDS = 3.0


class IndexerHandle:
    """Owns the writer connection, the rebuild/sync lock, and the background task."""

    def __init__(self, cfg: AppConfig, conn: sqlite3.Connection, client: JoplinClient) -> None:
        self.cfg = cfg
        self.conn = conn
        self.client = client
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._rebuild_in_progress = False
        self._last_result: IndexRunResult | None = None
        self._last_error: str | None = None

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

    async def run_full_rebuild(self) -> IndexRunResult:
        if self._rebuild_in_progress:
            raise IndexInProgressError("A full rebuild is already running.")
        async with self._lock:
            self._rebuild_in_progress = True
            try:
                result = await services.full_rebuild(self.conn, self.client, self.cfg)
                self._last_result = result
                self._last_error = result.message if result.status != "success" else None
                return result
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self._rebuild_in_progress = False

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

    async def _run_loop(self) -> None:
        log_event(logger, "service.start", component="indexer")
        while not self._stop.is_set():
            try:
                await self.run_incremental_once()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Incremental sync tick failed")
            # interval elapsed (TimeoutError) -> run the next tick; stop_event -> exit
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.joplin.event_poll_seconds
                )
        log_event(logger, "service.stop", component="indexer")


def create_indexer(cfg: AppConfig, conn: sqlite3.Connection, client: JoplinClient) -> IndexerHandle:
    """Build an :class:`IndexerHandle`; call ``await handle.start(...)`` to run the loop."""
    return IndexerHandle(cfg, conn, client)
