"""Response models for the HTTP API (PRD §13)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from pkm_sidecar.models import GraphEdge, GraphNode


class HealthResponse(BaseModel):
    ok: bool
    service: str
    version: str


class JoplinStatus(BaseModel):
    configured: bool
    reachable: bool
    base_url: str
    last_error: str | None = None


class DatabaseStatus(BaseModel):
    path: str
    note_count: int
    folder_count: int
    tag_count: int
    task_count: int
    resource_count: int = 0


class IndexingStatus(BaseModel):
    last_full_index_at: int | None = None
    last_incremental_index_at: int | None = None
    last_event_id: str | None = None
    # True while the derived tables (tasks/links/tags/resources) are wiped and not
    # yet refilled. Expected and transient during a rebuild; if it is set while
    # rebuild_in_progress is False, a previous rebuild was interrupted and the
    # views are serving partial data until one finishes.
    index_incomplete: bool = False
    rebuild_in_progress: bool = False


class RuntimeStatus(BaseModel):
    launched_by: str | None = None


class EnrichmentStatus(BaseModel):
    """Whether suggestions exist to review, so the dashboard can hide the column."""

    enabled: bool = False
    pending_titles: int = 0
    pending_tags: int = 0


class StatusResponse(BaseModel):
    service: str
    version: str
    joplin: JoplinStatus
    database: DatabaseStatus
    indexing: IndexingStatus
    runtime: RuntimeStatus
    enrichment: EnrichmentStatus = EnrichmentStatus()


class GraphResponse(BaseModel):
    """Note-to-note internal link graph (PRD v0.2 backlinks/graph)."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False


class NoteDetail(BaseModel):
    """Single-note view — the only response that carries the full body (PRD §13.2)."""

    id: str
    parent_id: str | None = None
    title: str
    body: str
    created_time: int | None = None
    updated_time: int | None = None
    user_created_time: int | None = None
    user_updated_time: int | None = None
    is_todo: bool = False
    todo_due: int | None = None
    todo_completed: int | None = None
    source_url: str | None = None


class SuggestionResponse(BaseModel):
    """One suggestion awaiting review.

    Carries ``current`` alongside ``proposed`` so review is a comparison rather
    than a leap of faith, and ``reason`` so a rule that produces bad suggestions
    can be identified from the queue itself rather than inferred.
    """

    id: int
    note_id: str
    kind: str
    proposed: dict[str, Any]
    current: dict[str, Any] | None = None
    reason: str | None = None
    confidence: float | None = None
    model: str
    stale: bool = False
    created_at: int


class DecisionResponse(BaseModel):
    """What a decision changed. Nothing in Joplin — this increment is read-only."""

    id: int
    decision: str
    wrote_to_joplin: bool = False
