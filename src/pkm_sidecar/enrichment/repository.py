"""Persistence for suggestions, decisions and embeddings (docs/enrichment.md §4).

Three rules from the plan live here rather than in the caller, because getting
them wrong is silent:

* **Identity covers every input.** :func:`compute_input_hash` folds in the body
  hash, the state being replaced, the model and the prompt version. Keying on
  ``notes.body_hash`` alone (it hashes the body only) would let a retitle reuse
  an old rejection.
* **Only the newest generation is offered.** A corpus re-queue adds a generation
  rather than replacing a row, so the old one stays servable; review must not
  then show both.
* **A regenerated payload you already rejected stays rejected.** Otherwise a
  drifting corpus re-asks the same question indefinitely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from pkm_sidecar import db as index_db
from pkm_sidecar.enrichment.models import (
    PROMPT_VERSION,
    DecisionValue,
    StoredEmbedding,
    Suggestion,
    SuggestionKind,
)

_SEP = "\x1f"  # unit separator: cannot occur in an id, title or model name


def compute_input_hash(
    *,
    kind: SuggestionKind,
    body_hash: str,
    current_state: str | Sequence[str] | None,
    model: str,
    prompt_version: int = PROMPT_VERSION,
) -> str:
    """Hash everything that can change the right answer for a note.

    *current_state* is the title for ``kind="title"`` and the note's tag ids for
    ``kind="tags"`` — the state the suggestion would replace. Tag ids are sorted
    so membership, not ordering, decides identity.

    The tag *corpus* is deliberately absent: folding it in would mean accepting
    one suggestion invalidates every other pending one. Corpus drift is handled
    by :meth:`SuggestionRepository.mark_stale_for_corpus` instead.
    """
    if current_state is None:
        state = ""
    elif isinstance(current_state, str):
        state = current_state
    else:
        state = ",".join(sorted(current_state))
    material = _SEP.join([kind, body_hash, state, model, str(prompt_version)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def _row_to_suggestion(row: sqlite3.Row) -> Suggestion:
    return Suggestion(
        id=row["id"],
        note_id=row["note_id"],
        kind=row["kind"],
        input_hash=row["input_hash"],
        payload=json.loads(row["payload"]),
        current_value=json.loads(row["current_value"]) if row["current_value"] else None,
        note_body_hash=row["note_body_hash"],
        note_updated_time=row["note_updated_time"],
        corpus_revision=row["corpus_revision"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        confidence=row["confidence"],
        reason=row["reason"],
        stale=bool(row["stale"]),
        generation=row["generation"],
        superseded_by=row["superseded_by"],
        decision=row["decision"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
    )


class SuggestionRepository:
    """All SQL over the enrichment store, bound to one connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with index_db.transaction(self.conn) as conn:
            yield conn

    # --- suggestions -------------------------------------------------------

    def record(self, suggestion: Suggestion) -> Suggestion:
        """Insert a suggestion, or return the existing row with the same identity.

        A payload identical to one already rejected for this note and kind is
        stored already-rejected, so corpus churn cannot re-ask a settled question.
        """
        existing = self.find_by_identity(
            suggestion.note_id, suggestion.kind, suggestion.input_hash, suggestion.generation
        )
        if existing is not None:
            return existing

        decision = suggestion.decision
        decided_at = suggestion.decided_at
        if decision is None and self.was_payload_rejected(
            suggestion.note_id, suggestion.kind, suggestion.payload
        ):
            decision, decided_at = "rejected", _now()

        created_at = suggestion.created_at or _now()
        cur = self.conn.execute(
            """
            INSERT INTO suggestions(
                note_id, kind, input_hash, payload, current_value, note_body_hash,
                note_updated_time, corpus_revision, model, prompt_version, confidence,
                reason, stale, generation, superseded_by, decision, decided_at, created_at)
            VALUES (:note_id, :kind, :input_hash, :payload, :current_value, :note_body_hash,
                :note_updated_time, :corpus_revision, :model, :prompt_version, :confidence,
                :reason, :stale, :generation, :superseded_by, :decision, :decided_at, :created_at)
            """,
            {
                "note_id": suggestion.note_id,
                "kind": suggestion.kind,
                "input_hash": suggestion.input_hash,
                "payload": json.dumps(suggestion.payload, sort_keys=True),
                "current_value": (
                    json.dumps(suggestion.current_value, sort_keys=True)
                    if suggestion.current_value is not None
                    else None
                ),
                "note_body_hash": suggestion.note_body_hash,
                "note_updated_time": suggestion.note_updated_time,
                "corpus_revision": suggestion.corpus_revision,
                "model": suggestion.model,
                "prompt_version": suggestion.prompt_version,
                "confidence": suggestion.confidence,
                "reason": suggestion.reason,
                "stale": 1 if suggestion.stale else 0,
                "generation": suggestion.generation,
                "superseded_by": suggestion.superseded_by,
                "decision": decision,
                "decided_at": decided_at,
                "created_at": created_at,
            },
        )
        stored = suggestion.model_copy(
            update={
                "id": int(cur.lastrowid or 0),
                "decision": decision,
                "decided_at": decided_at,
                "created_at": created_at,
            }
        )
        return stored

    def next_generation(self, note_id: str, kind: SuggestionKind, input_hash: str) -> int:
        row = self.conn.execute(
            "SELECT max(generation) AS g FROM suggestions "
            "WHERE note_id = ? AND kind = ? AND input_hash = ?",
            (note_id, kind, input_hash),
        ).fetchone()
        return 1 if row is None or row["g"] is None else int(row["g"]) + 1

    def supersede(self, old_id: int, new_id: int) -> None:
        """Point a replaced row at its replacement; it stops being offered for review."""
        self.conn.execute("UPDATE suggestions SET superseded_by = ? WHERE id = ?", (new_id, old_id))

    def find_by_identity(
        self, note_id: str, kind: SuggestionKind, input_hash: str, generation: int
    ) -> Suggestion | None:
        row = self.conn.execute(
            "SELECT * FROM suggestions "
            "WHERE note_id = ? AND kind = ? AND input_hash = ? AND generation = ?",
            (note_id, kind, input_hash, generation),
        ).fetchone()
        return None if row is None else _row_to_suggestion(row)

    def current_for_note(self, note_id: str, kind: SuggestionKind) -> Suggestion | None:
        """The newest, non-superseded suggestion for a note."""
        row = self.conn.execute(
            "SELECT * FROM suggestions WHERE note_id = ? AND kind = ? AND superseded_by IS NULL "
            "ORDER BY generation DESC, id DESC LIMIT 1",
            (note_id, kind),
        ).fetchone()
        return None if row is None else _row_to_suggestion(row)

    def pending(self, kind: SuggestionKind | None = None, limit: int = 100) -> list[Suggestion]:
        """Undecided, non-superseded suggestions for notes that have not opted out.

        Ordered by confidence so a long review queue starts with its best guesses
        rather than whatever was generated first.
        """
        clause = "AND s.kind = ?" if kind is not None else ""
        params: list[Any] = [kind] if kind is not None else []
        rows = self.conn.execute(
            f"""
            SELECT s.* FROM suggestions s
            WHERE s.decision IS NULL AND s.superseded_by IS NULL {clause}
              AND NOT EXISTS (
                  SELECT 1 FROM opt_outs o WHERE o.note_id = s.note_id AND o.kind = s.kind)
            ORDER BY s.confidence IS NULL, s.confidence DESC, s.id ASC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [_row_to_suggestion(r) for r in rows]

    def decide(self, suggestion_id: int, decision: DecisionValue) -> None:
        self.conn.execute(
            "UPDATE suggestions SET decision = ?, decided_at = ? WHERE id = ?",
            (decision, _now(), suggestion_id),
        )

    def was_payload_rejected(
        self, note_id: str, kind: SuggestionKind, payload: dict[str, Any]
    ) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM suggestions WHERE note_id = ? AND kind = ? "
            "AND decision = 'rejected' AND payload = ? LIMIT 1",
            (note_id, kind, json.dumps(payload, sort_keys=True)),
        ).fetchone()
        return row is not None

    def mark_stale_for_corpus(self, kind: SuggestionKind, corpus_revision: str) -> int:
        """Flag suggestions generated against a different corpus revision.

        They stay servable — staleness asks for regeneration, it does not hide a
        suggestion that is probably still right.
        """
        return self.conn.execute(
            "UPDATE suggestions SET stale = 1 WHERE kind = ? AND superseded_by IS NULL "
            "AND decision IS NULL AND corpus_revision IS NOT NULL AND corpus_revision != ?",
            (kind, corpus_revision),
        ).rowcount

    def stale_suggestions(self, kind: SuggestionKind, limit: int = 100) -> list[Suggestion]:
        rows = self.conn.execute(
            "SELECT * FROM suggestions WHERE kind = ? AND stale = 1 AND superseded_by IS NULL "
            "AND decision IS NULL ORDER BY id LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [_row_to_suggestion(r) for r in rows]

    # --- opt-outs ----------------------------------------------------------

    def opt_out(self, note_id: str, kind: SuggestionKind) -> None:
        self.conn.execute(
            "INSERT INTO opt_outs(note_id, kind, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(note_id, kind) DO NOTHING",
            (note_id, kind, _now()),
        )

    def clear_opt_out(self, note_id: str, kind: SuggestionKind) -> None:
        self.conn.execute("DELETE FROM opt_outs WHERE note_id = ? AND kind = ?", (note_id, kind))

    def is_opted_out(self, note_id: str, kind: SuggestionKind) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM opt_outs WHERE note_id = ? AND kind = ?", (note_id, kind)
        ).fetchone()
        return row is not None

    # --- embeddings --------------------------------------------------------

    def put_embedding(
        self, note_id: str, *, model: str, body_hash: str, vector: Sequence[float]
    ) -> None:
        import struct

        blob = struct.pack(f"<{len(vector)}f", *vector)
        self.conn.execute(
            """
            INSERT INTO note_embeddings(note_id, model, body_hash, dimensions, vector, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id, model) DO UPDATE SET
                body_hash=excluded.body_hash, dimensions=excluded.dimensions,
                vector=excluded.vector, created_at=excluded.created_at
            """,
            (note_id, model, body_hash, len(vector), blob, _now()),
        )

    def get_embedding(self, note_id: str, *, model: str, body_hash: str) -> list[float] | None:
        """Return the cached vector only if *both* model and body_hash still match.

        Vectors from different embedding models are not comparable, so reusing one
        across a model change would corrupt every nearest-neighbour result without
        any visible error.
        """
        import struct

        row = self.conn.execute(
            "SELECT body_hash, dimensions, vector FROM note_embeddings "
            "WHERE note_id = ? AND model = ?",
            (note_id, model),
        ).fetchone()
        if row is None or row["body_hash"] != body_hash:
            return None
        return list(struct.unpack(f"<{row['dimensions']}f", row["vector"]))

    def embedding_meta(self, note_id: str, *, model: str) -> StoredEmbedding | None:
        row = self.conn.execute(
            "SELECT note_id, model, body_hash, dimensions, created_at FROM note_embeddings "
            "WHERE note_id = ? AND model = ?",
            (note_id, model),
        ).fetchone()
        return None if row is None else StoredEmbedding(**dict(row))

    def count_embeddings(self, *, model: str) -> int:
        row = self.conn.execute(
            "SELECT count(*) AS c FROM note_embeddings WHERE model = ?", (model,)
        ).fetchone()
        return int(row["c"])
