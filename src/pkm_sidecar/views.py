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
    DecisionResponse,
    EnrichmentStatus,
    GraphResponse,
    IndexingStatus,
    JoplinStatus,
    NoteDetail,
    RuntimeStatus,
    StatusResponse,
    SuggestionResponse,
)
from pkm_sidecar.enrichment import db as enrichment_db
from pkm_sidecar.enrichment.models import DecisionValue, Suggestion
from pkm_sidecar.enrichment.repository import SuggestionRepository
from pkm_sidecar.errors import ConflictError, NotFoundError, scrub_token
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


def get_suggestions(request: Request) -> Iterator[SuggestionRepository | None]:
    """Per-request enrichment store, or None when the feature is off.

    None rather than an error: the review endpoints then answer "nothing to
    review", which is true and lets the dashboard hide its column without
    special-casing a failure.
    """
    path = getattr(request.app.state, "suggestions_path", None)
    if path is None:
        yield None
        return
    conn = enrichment_db.open_connection(path)
    try:
        yield SuggestionRepository(conn)
    finally:
        conn.close()


def _to_response(suggestion: Suggestion) -> SuggestionResponse:
    return SuggestionResponse(
        id=suggestion.id or 0,
        note_id=suggestion.note_id,
        kind=suggestion.kind,
        proposed=suggestion.payload,
        current=suggestion.current_value,
        reason=suggestion.reason,
        confidence=suggestion.confidence,
        model=suggestion.model,
        stale=suggestion.stale,
        created_at=suggestion.created_at,
    )


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
        incomplete = db.rebuild_in_progress_since(reader) is not None
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
            index_incomplete=incomplete,
            rebuild_in_progress=indexer.rebuild_in_progress,
        ),
        runtime=RuntimeStatus(launched_by=os.environ.get("JOPLIN_SIDECAR_LAUNCHED_BY")),
        enrichment=_enrichment_status(request),
    )


def _enrichment_status(request: Request) -> EnrichmentStatus:
    path = getattr(request.app.state, "suggestions_path", None)
    if path is None:
        return EnrichmentStatus(enabled=False)
    conn = enrichment_db.open_connection(path, read_only=True)
    try:
        repo = SuggestionRepository(conn)
        return EnrichmentStatus(
            enabled=True,
            pending_titles=repo.count_pending("title"),
            pending_tags=repo.count_pending("tags"),
        )
    finally:
        conn.close()


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


@router.get("/notes/review", response_model=list[NoteSummary])
async def notes_review(
    request: Request,
    repo: NoteRepository = Depends(get_repo),
    days: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
) -> list[NoteSummary]:
    effective_days = days if days is not None else request.app.state.settings.indexing.stale_days
    return repo.fetch_review(days=effective_days, limit=limit)


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


# --- suggestion review (docs/enrichment.md §7) -----------------------------
#
# Review only. Accepting records a decision and writes **nothing** to Joplin —
# the apply path is a deliberately separate increment, so the worst a mistaken
# click can do here is mislabel a row in a database you can delete.


@router.get("/suggestions", response_model=list[SuggestionResponse])
async def list_suggestions(
    store: SuggestionRepository | None = Depends(get_suggestions),
    kind: str | None = Query(None, pattern="^(title|tags)$"),
    limit: int = Query(50, ge=1, le=500),
) -> list[SuggestionResponse]:
    """Undecided suggestions, best guesses first."""
    if store is None:
        return []
    suggestions = store.pending(kind, limit=limit)  # type: ignore[arg-type]
    return [_to_response(s) for s in suggestions]


@router.post("/suggestions/{suggestion_id}/accept", response_model=DecisionResponse)
async def accept_suggestion(
    suggestion_id: int, store: SuggestionRepository | None = Depends(get_suggestions)
) -> DecisionResponse:
    return _decide(store, suggestion_id, "accepted")


@router.post("/suggestions/{suggestion_id}/reject", response_model=DecisionResponse)
async def reject_suggestion(
    suggestion_id: int, store: SuggestionRepository | None = Depends(get_suggestions)
) -> DecisionResponse:
    return _decide(store, suggestion_id, "rejected")


def _decide(
    store: SuggestionRepository | None, suggestion_id: int, decision: DecisionValue
) -> DecisionResponse:
    if store is None:
        raise NotFoundError("Enrichment is not enabled, so there is nothing to decide.")
    # Read and write inside one BEGIN IMMEDIATE, so two tabs cannot both pass the
    # check and then both write.
    with store.transaction():
        existing = store.get(suggestion_id)
        if existing is None:
            raise NotFoundError(f"No suggestion {suggestion_id}.")
        if not store.is_pending(existing):
            raise ConflictError(
                f"Suggestion {suggestion_id} is no longer pending "
                f"({'superseded' if existing.superseded_by else existing.decision}). "
                "Reload the queue."
            )
        store.decide(suggestion_id, decision)
    return DecisionResponse(id=suggestion_id, decision=decision, wrote_to_joplin=False)


@router.post("/suggestions/{suggestion_id}/dismiss", status_code=202)
async def dismiss_note(
    suggestion_id: int, store: SuggestionRepository | None = Depends(get_suggestions)
) -> dict[str, object]:
    """Reject this suggestion *and* stop suggesting this kind for the note.

    Distinct from rejecting one proposal: "my title is fine, stop asking" has to
    survive a body edit, a model change and a prompt bump, which a rejection
    deliberately does not.
    """
    if store is None:
        raise NotFoundError("Enrichment is not enabled, so there is nothing to dismiss.")
    with store.transaction():
        suggestion = store.get(suggestion_id)
        if suggestion is None:
            raise NotFoundError(f"No suggestion {suggestion_id}.")
        if not store.is_pending(suggestion):
            # Opting a note out for ever on the strength of an obsolete row is the
            # worst version of this bug: the decision is permanent.
            raise ConflictError(
                f"Suggestion {suggestion_id} is no longer pending. Reload the queue."
            )
        store.decide(suggestion_id, "rejected")
        store.opt_out(suggestion.note_id, suggestion.kind)
    return {"accepted": True, "note_id": suggestion.note_id, "kind": suggestion.kind}
