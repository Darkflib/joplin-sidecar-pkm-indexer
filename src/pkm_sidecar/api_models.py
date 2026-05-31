"""Response models for the HTTP API (PRD §13)."""

from __future__ import annotations

from pydantic import BaseModel


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


class RuntimeStatus(BaseModel):
    launched_by: str | None = None


class StatusResponse(BaseModel):
    service: str
    version: str
    joplin: JoplinStatus
    database: DatabaseStatus
    indexing: IndexingStatus
    runtime: RuntimeStatus


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
