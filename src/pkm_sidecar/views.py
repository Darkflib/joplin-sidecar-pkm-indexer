"""API routes (PRD §13). Step 11 ships /api/status; later steps add the rest.

The ``/api`` router carries a bearer-token dependency, so every route under it is
authenticated. ``/health`` is mounted publicly on the app in app.py.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, Request

from pkm_sidecar import __version__, db
from pkm_sidecar.api_models import (
    DatabaseStatus,
    IndexingStatus,
    JoplinStatus,
    RuntimeStatus,
    StatusResponse,
)
from pkm_sidecar.errors import scrub_token
from pkm_sidecar.repositories import NoteRepository
from pkm_sidecar.security import verify_bearer_token

SERVICE_NAME = "pkm-sidecar"
_PING_CACHE_SECONDS = 5.0


async def require_bearer(request: Request) -> None:
    """Bearer-auth dependency; reads the expected token from app state."""
    expected = getattr(request.app.state, "api_token", None)
    verify_bearer_token(request.headers.get("authorization"), expected)


router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer)])


async def _cached_reachable(request: Request) -> bool:
    """Joplin reachability, cached for 5s so /api/status stays cheap."""
    cache = request.app.state.joplin_cache
    now = time.monotonic()
    if now - cache["ts"] < _PING_CACHE_SECONDS:
        return bool(cache["reachable"])
    reachable = bool(await request.app.state.client.ping())
    cache["ts"] = now
    cache["reachable"] = reachable
    return reachable


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value is not None else None


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse:
    cfg = request.app.state.settings
    reader = db.open_reader_connection(cfg.database.path)
    try:
        repo = NoteRepository(reader)
        counts = repo.get_status_counts()
        last_full = repo.get_meta(db.META_LAST_FULL_INDEX_AT)
        last_inc = repo.get_meta(db.META_LAST_INCREMENTAL_INDEX_AT)
        last_event = repo.get_meta(db.META_LAST_EVENT_ID)
    finally:
        reader.close()

    reachable = await _cached_reachable(request)
    indexer = request.app.state.indexer
    return StatusResponse(
        service=SERVICE_NAME,
        version=__version__,
        joplin=JoplinStatus(
            configured=bool(cfg.joplin.token),
            reachable=reachable,
            base_url=scrub_token(cfg.joplin.base_url),
            last_error=indexer.last_error,
        ),
        database=DatabaseStatus(
            path=str(cfg.database.path),
            note_count=counts.note_count,
            folder_count=counts.folder_count,
            tag_count=counts.tag_count,
            task_count=counts.task_count,
        ),
        indexing=IndexingStatus(
            last_full_index_at=_int_or_none(last_full),
            last_incremental_index_at=_int_or_none(last_inc),
            last_event_id=last_event,
        ),
        runtime=RuntimeStatus(launched_by=os.environ.get("JOPLIN_SIDECAR_LAUNCHED_BY")),
    )
