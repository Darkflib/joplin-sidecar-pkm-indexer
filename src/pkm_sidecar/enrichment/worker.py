"""Walk title candidates, propose titles, store suggestions (docs/enrichment.md §7).

A plain serial queue. The measured workload is an overnight batch of ~87 model
calls at roughly a second each, then a handful of notes a day — so there is
nothing here to parallelise, and the absence of concurrency is the design rather
than a gap in it.

Runs off the request path and off the incremental tick: a multi-second model call
must never block the event loop or delay Joplin sync.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from pkm_sidecar.config import AppConfig
from pkm_sidecar.enrichment.models import Suggestion
from pkm_sidecar.enrichment.ollama_client import OllamaClient
from pkm_sidecar.enrichment.repository import SuggestionRepository, compute_input_hash
from pkm_sidecar.enrichment.title_candidates import TitleCandidate, find_candidates
from pkm_sidecar.enrichment.title_generator import (
    PROMPT_VERSION,
    build_prompt,
    clean_title,
    propose_title,
)
from pkm_sidecar.errors import OllamaError
from pkm_sidecar.logging_config import get_logger, log_event

logger = get_logger("enrichment")

# Recorded as the "model" for a suggestion the URL produced. It is part of the
# suggestion identity, so a later model pass over the same note yields a
# different input_hash and can coexist rather than colliding.
SLUG_SOURCE = "slug"
# A slug title is derived from the page's own URL rather than guessed, so it
# sorts above model output in a review queue ordered best-first.
SLUG_CONFIDENCE = 0.9


@dataclass
class TitleRunResult:
    considered: int = 0
    opted_out: int = 0
    nothing_to_work_from: int = 0
    already_suggested: int = 0
    from_slug: int = 0
    from_model: int = 0
    rejected: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def stored(self) -> int:
        return self.from_slug + self.from_model


def _bodies_for(conn: sqlite3.Connection, note_ids: list[str]) -> dict[str, str]:
    """Full bodies for the notes about to be generated for.

    Candidate detection only reads a 1000-character head, which is enough for its
    regexes but not for a prompt that wants the note's head *and* tail.
    """
    out: dict[str, str] = {}
    for chunk_start in range(0, len(note_ids), 500):  # keep the IN list well under SQLite's cap
        chunk = note_ids[chunk_start : chunk_start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT id, body FROM notes WHERE id IN ({placeholders})", tuple(chunk)
        ):
            out[row["id"]] = row["body"] or ""
    return out


def _suggestion_for(
    candidate: TitleCandidate,
    *,
    title: str,
    model: str,
    confidence: float | None,
    input_hash: str,
    generation: int,
) -> Suggestion:
    return Suggestion(
        note_id=candidate.note_id,
        kind="title",
        input_hash=input_hash,
        payload={"title": title},
        current_value={"title": candidate.title},
        note_body_hash=candidate.body_hash,
        # Carried now so the write-back increment can refuse to clobber an edit
        # made since the suggestion was generated.
        note_updated_time=candidate.updated_time,
        model=model,
        prompt_version=PROMPT_VERSION,
        confidence=confidence,
        reason=candidate.issue,
        generation=generation,
    )


async def generate_titles(
    *,
    index_conn: sqlite3.Connection,
    repo: SuggestionRepository,
    cfg: AppConfig,
    client: OllamaClient | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> TitleRunResult:
    """Propose a title for every candidate that needs and can have one.

    The offline slug path runs first and needs no *client* at all, so a run with
    ``client=None`` still does useful work — which matters, because for a dead
    bookmark the URL is the only title source that will ever exist.
    """
    result = TitleRunResult()
    candidates = find_candidates(index_conn, limit=limit)
    bodies = _bodies_for(index_conn, [c.note_id for c in candidates])

    for candidate in candidates:
        result.considered += 1
        if repo.is_opted_out(candidate.note_id, "title"):
            result.opted_out += 1
            continue

        body = bodies.get(candidate.note_id, "")
        proposed, source = propose_title(issue=candidate.issue, title=candidate.title, body=body)
        model = SLUG_SOURCE if source == "slug" else cfg.enrichment.title_model
        confidence = SLUG_CONFIDENCE if source == "slug" else None

        if source == "model" and not candidate.generatable:
            # Flagged as a defect, but a screenshot or a bare link gives a model
            # nothing to work from; it would invent something confident.
            result.nothing_to_work_from += 1
            continue

        input_hash = compute_input_hash(
            kind="title",
            body_hash=candidate.body_hash,
            current_state=candidate.title,
            model=model,
        )
        generation = repo.next_generation(candidate.note_id, "title", input_hash)
        if generation > 1:
            # Same note, same body, same title, same model, same prompt: the
            # answer cannot have changed, so do not spend a model call on it.
            result.already_suggested += 1
            continue

        if source == "model":
            if client is None:
                result.failed += 1
                result.errors.append(f"{candidate.note_id}: no model client configured")
                continue
            try:
                completion = await client.generate(model, build_prompt(body))
            except OllamaError as exc:
                result.failed += 1
                result.errors.append(f"{candidate.note_id}: {type(exc).__name__}")
                continue
            proposed = clean_title(completion.response, current_title=candidate.title)

        if not proposed:
            result.rejected += 1
            continue

        if not dry_run:
            with repo.transaction():
                repo.record(
                    _suggestion_for(
                        candidate,
                        title=proposed,
                        model=model,
                        confidence=confidence,
                        input_hash=input_hash,
                        generation=generation,
                    )
                )
        if source == "slug":
            result.from_slug += 1
        else:
            result.from_model += 1

    log_event(
        logger,
        "index.full.completed" if not dry_run else "index.full.started",
        component="enrichment.titles",
        considered=result.considered,
        stored=result.stored,
        slug=result.from_slug,
        model=result.from_model,
        skipped=result.nothing_to_work_from,
        rejected=result.rejected,
        failed=result.failed,
    )
    return result
