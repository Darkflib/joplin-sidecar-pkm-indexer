"""API routes (PRD §13). Step 11 ships /api/status; later steps add the rest.

The ``/api`` router carries a bearer-token dependency, so every route under it is
authenticated. ``/health`` is mounted publicly on the app in app.py.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pkm_sidecar import __version__, db
from pkm_sidecar.api_models import (
    DatabaseStatus,
    GraphResponse,
    IndexingStatus,
    JoplinStatus,
    NoteDetail,
    RuntimeStatus,
    StatusResponse,
)
from pkm_sidecar.errors import NotFoundError, scrub_token
from pkm_sidecar.models import ExtractedLink, ExtractedTask, NoteSummary, ResourceRow, SearchHit
from pkm_sidecar.repositories import NoteRepository
from pkm_sidecar.security import verify_bearer_token

SERVICE_NAME = "pkm-sidecar"
_PING_CACHE_SECONDS = 5.0


async def require_bearer(request: Request) -> None:
    """Bearer-auth dependency; reads the expected token from app state."""
    expected = getattr(request.app.state, "api_token", None)
    verify_bearer_token(request.headers.get("authorization"), expected)


router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer)])


def get_repo(request: Request) -> Iterator[NoteRepository]:
    """Per-request read-only repository (closed when the request finishes)."""
    reader = db.open_reader_connection(request.app.state.settings.database.path)
    try:
        yield NoteRepository(reader)
    finally:
        reader.close()


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
            resource_count=counts.resource_count,
        ),
        indexing=IndexingStatus(
            last_full_index_at=_int_or_none(last_full),
            last_incremental_index_at=_int_or_none(last_inc),
            last_event_id=last_event,
        ),
        runtime=RuntimeStatus(launched_by=os.environ.get("JOPLIN_SIDECAR_LAUNCHED_BY")),
    )


# --- workflow views --------------------------------------------------------


@router.get("/notes/recent", response_model=list[NoteSummary])
async def notes_recent(
    repo: NoteRepository = Depends(get_repo), limit: int = Query(50, ge=1, le=500)
) -> list[NoteSummary]:
    return repo.fetch_recent(limit=limit)


@router.get("/notes/inbox", response_model=list[NoteSummary])
async def notes_inbox(
    request: Request,
    repo: NoteRepository = Depends(get_repo),
    limit: int = Query(100, ge=1, le=500),
) -> list[NoteSummary]:
    names = request.app.state.settings.indexing.inbox_folder_names
    return repo.fetch_inbox(names, limit=limit)


@router.get("/notes/untagged", response_model=list[NoteSummary])
async def notes_untagged(
    repo: NoteRepository = Depends(get_repo), limit: int = Query(100, ge=1, le=500)
) -> list[NoteSummary]:
    return repo.fetch_untagged(limit=limit)


@router.get("/notes/todos", response_model=list[NoteSummary])
async def notes_todos(
    repo: NoteRepository = Depends(get_repo), limit: int = Query(100, ge=1, le=500)
) -> list[NoteSummary]:
    return repo.fetch_todos(limit=limit)


@router.get("/notes/stale", response_model=list[NoteSummary])
async def notes_stale(
    request: Request,
    repo: NoteRepository = Depends(get_repo),
    days: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
) -> list[NoteSummary]:
    effective_days = days if days is not None else request.app.state.settings.indexing.stale_days
    return repo.fetch_stale(days=effective_days, limit=limit)


@router.get("/search", response_model=list[SearchHit])
async def search(
    repo: NoteRepository = Depends(get_repo),
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=500),
) -> list[SearchHit]:
    if not q.strip():
        raise HTTPException(status_code=422, detail="q must not be blank")
    return repo.fts_search(q, limit=limit)


@router.get("/note/{note_id}", response_model=NoteDetail)
async def note_detail(note_id: str, repo: NoteRepository = Depends(get_repo)) -> NoteDetail:
    row = repo.get_note(note_id)
    if row is None or row.deleted:
        raise NotFoundError(f"Note {note_id} is not in the index.")
    return NoteDetail(**row.model_dump(exclude={"body_hash", "indexed_at", "deleted"}))


@router.get("/note/{note_id}/tasks", response_model=list[ExtractedTask])
async def note_tasks(note_id: str, repo: NoteRepository = Depends(get_repo)) -> list[ExtractedTask]:
    return repo.get_tasks_for_note(note_id)


@router.get("/note/{note_id}/links", response_model=list[ExtractedLink])
async def note_links(note_id: str, repo: NoteRepository = Depends(get_repo)) -> list[ExtractedLink]:
    return repo.get_links_for_note(note_id)


@router.get("/note/{note_id}/resources", response_model=list[ResourceRow])
async def note_resources(
    note_id: str, repo: NoteRepository = Depends(get_repo)
) -> list[ResourceRow]:
    return repo.get_resources_for_note(note_id)


@router.get("/note/{note_id}/backlinks", response_model=list[NoteSummary])
async def note_backlinks(
    note_id: str, repo: NoteRepository = Depends(get_repo), limit: int = Query(100, ge=1, le=500)
) -> list[NoteSummary]:
    return repo.get_backlinks(note_id, limit=limit)


@router.get("/graph", response_model=GraphResponse)
async def graph(
    repo: NoteRepository = Depends(get_repo), limit: int = Query(2000, ge=1, le=20000)
) -> GraphResponse:
    nodes, edges, truncated = repo.get_graph(limit=limit)
    return GraphResponse(nodes=nodes, edges=edges, truncated=truncated)


# --- index commands --------------------------------------------------------


@router.post("/index/rebuild", status_code=202)
async def index_rebuild(request: Request) -> dict[str, object]:
    run_id, started_at = request.app.state.indexer.dispatch_rebuild()
    return {"run_id": run_id, "mode": "full", "started_at": started_at}


@router.post("/index/sync", status_code=202)
async def index_sync(request: Request) -> dict[str, object]:
    request.app.state.indexer.dispatch_sync()
    return {"accepted": True}


@router.post("/index/note/{note_id}", status_code=202)
async def index_note(note_id: str, request: Request) -> dict[str, object]:
    found = await request.app.state.indexer.reindex_one(note_id)
    if not found:
        raise NotFoundError(f"Joplin has no note {note_id}.")
    return {"accepted": True, "note_id": note_id}
