"""Models for the enrichment store (docs/enrichment.md §4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

SuggestionKind = Literal["title", "tags"]
DecisionValue = Literal["accepted", "rejected"]

# Bumped when a prompt changes in a way that should invalidate cached output.
# Part of input_hash, so a bump regenerates rather than colliding.
PROMPT_VERSION = 1


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
    prompt_version: int = PROMPT_VERSION
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
