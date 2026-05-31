"""Pydantic models shared across the persistence and API layers (PRD §9, §13).

``NoteSummary`` and ``SearchHit`` deliberately omit the note body — list and
search views never carry full bodies (defence in depth against leaking content
and against bloating responses). Only ``NoteRow`` (and the single-note endpoint)
expose ``body``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

LinkType = Literal["internal_joplin", "external_url", "relative", "other"]


class FolderRow(BaseModel):
    id: str
    parent_id: str | None = None
    title: str
    created_time: int | None = None
    updated_time: int | None = None
    indexed_at: int


class NoteRow(BaseModel):
    id: str
    parent_id: str | None = None
    title: str
    body: str
    body_hash: str
    created_time: int | None = None
    updated_time: int | None = None
    user_created_time: int | None = None
    user_updated_time: int | None = None
    is_todo: bool = False
    todo_due: int | None = None
    todo_completed: int | None = None
    source_url: str | None = None
    indexed_at: int
    deleted: bool = False


class NoteSummary(BaseModel):
    """A note for list views — no ``body`` field by design."""

    id: str
    parent_id: str | None = None
    title: str
    updated_time: int | None = None
    user_updated_time: int | None = None
    is_todo: bool = False
    todo_completed: int | None = None
    snippet: str | None = None


class TagRow(BaseModel):
    id: str
    title: str
    created_time: int | None = None
    updated_time: int | None = None
    indexed_at: int


class ExtractedTask(BaseModel):
    id: int | None = None
    note_id: str
    line_number: int
    checked: bool
    text: str
    raw_line: str


class ExtractedLink(BaseModel):
    id: int | None = None
    note_id: str
    line_number: int | None = None
    link_text: str | None = None
    target: str
    link_type: LinkType


class SearchHit(BaseModel):
    id: str
    title: str
    snippet: str
    updated_time: int | None = None
    parent_id: str | None = None


class StatusCounts(BaseModel):
    note_count: int = 0
    folder_count: int = 0
    tag_count: int = 0
    task_count: int = 0
    deleted_count: int = 0
