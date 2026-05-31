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
