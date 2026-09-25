"""Models for the enrichment store (docs/enrichment.md §4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

SuggestionKind = Literal["title", "tags"]
DecisionValue = Literal["accepted", "rejected"]


class Suggestion(BaseModel):
    """One generated suggestion, at one generation, for one note."""

    id: int | None = None
    note_id: str
    kind: SuggestionKind
    input_hash: str
    payload: dict[str, Any]
    current_value: dict[str, Any] | None = None
    note_body_hash: str
    note_updated_time: int | None = None
    corpus_revision: str | None = None
    model: str
    # No default on purpose. Prompt versions are per-kind (a title prompt and a
    # tag prompt change independently), and a shared default here silently
    # diverged from the one the worker stored — a bump then changed the recorded
    # version without changing input_hash, so nothing regenerated.
    prompt_version: int
    confidence: float | None = None
    reason: str | None = None
    stale: bool = False
    generation: int = 1
    superseded_by: int | None = None
    decision: DecisionValue | None = None
    decided_at: int | None = None
    created_at: int = 0


class StoredEmbedding(BaseModel):
    note_id: str
    model: str
    body_hash: str
    dimensions: int
    created_at: int
