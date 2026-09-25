"""Embedding backfill and tag retrieval (docs/enrichment.md §6).

Embeddings do **recall**; the model does precision. This module is the recall
half: embed every note once, then for an untagged note find its nearest tagged
neighbours and let their tags vote.

The scoring here exists because of a measured failure, not a theoretical one. On
a small corpus, raw summed similarity put `billing` top for a note about
key-encryption-key rotation, where `openbao` and `security` are correct: tags
attached to more notes, or to notes written in generically ops-flavoured
language, win regardless of topic. :func:`score_tags` divides that out.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

from pkm_sidecar.config import AppConfig
from pkm_sidecar.enrichment.ollama_client import OllamaClient
from pkm_sidecar.enrichment.repository import SuggestionRepository
from pkm_sidecar.errors import OllamaError
from pkm_sidecar.logging_config import get_logger, log_event

logger = get_logger("enrichment")

# bge-large and friends truncate around 512 tokens, so sending more costs time
# and changes nothing. Title first: it is the strongest short signal a note has.
EMBED_CHARS = 1800
# Measured at ~30 ms/doc batched; batching is what makes a 2,240-note backfill a
# minute rather than twenty.
BATCH_SIZE = 32


@dataclass
class EmbedRunResult:
    considered: int = 0
    embedded: int = 0
    cached: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class TagScore:
    tag_id: str
    title: str
    score: float
    supporters: int  # how many neighbours carried it


def embedding_text(title: str, body: str) -> str:
    """What actually gets embedded for a note."""
    joined = f"{title.strip()}\n\n{body.strip()}".strip()
    return joined[:EMBED_CHARS]


async def backfill_embeddings(
    *,
    index_conn: sqlite3.Connection,
    repo: SuggestionRepository,
    client: OllamaClient,
    cfg: AppConfig,
    limit: int | None = None,
) -> EmbedRunResult:
    """Embed every note that does not already have a current vector.

    Cached on ``(note_id, model, body_hash)``, so a second run is free and a
    changed note is re-embedded. Failures are counted per batch rather than
    aborting: one bad note must not end an overnight backfill.
    """
    model = cfg.enrichment.embedding_model
    result = EmbedRunResult()
    sql = (
        "SELECT id, title, body, body_hash FROM notes WHERE deleted = 0 ORDER BY updated_time DESC"
    )
    pending: list[tuple[str, str, str]] = []  # (note_id, body_hash, text)

    async def flush() -> None:
        if not pending:
            return
        try:
            vectors = await client.embed(model, [text for _, _, text in pending])
        except OllamaError as exc:
            result.failed += len(pending)
            result.errors.append(f"batch of {len(pending)}: {type(exc).__name__}")
            pending.clear()
            return
        with repo.transaction():
            for (note_id, body_hash, _), vector in zip(pending, vectors, strict=True):
                repo.put_embedding(note_id, model=model, body_hash=body_hash, vector=vector)
        result.embedded += len(pending)
        pending.clear()

    for row in index_conn.execute(sql):
        if limit is not None and result.considered >= limit:
            break
        result.considered += 1
        if repo.get_embedding(row["id"], model=model, body_hash=row["body_hash"]) is not None:
            result.cached += 1
            continue
        pending.append(
            (row["id"], row["body_hash"], embedding_text(row["title"] or "", row["body"] or ""))
        )
        if len(pending) >= BATCH_SIZE:
            await flush()
    await flush()

    log_event(
        logger,
        "enrichment.embeddings.completed",
        model=model,
        considered=result.considered,
        embedded=result.embedded,
        cached=result.cached,
        failed=result.failed,
    )
    return result


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def tagged_notes(index_conn: sqlite3.Connection) -> dict[str, list[str]]:
    """note_id -> tag ids, for live notes and live tags only."""
    out: dict[str, list[str]] = {}
    for row in index_conn.execute(
        "SELECT nt.note_id, nt.tag_id FROM note_tags nt "
        "JOIN notes n ON n.id = nt.note_id AND n.deleted = 0 "
        "JOIN tags t ON t.id = nt.tag_id AND t.deleted = 0"
    ):
        out.setdefault(row["note_id"], []).append(row["tag_id"])
    return out


def tag_titles(index_conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["id"]: r["title"]
        for r in index_conn.execute("SELECT id, title FROM tags WHERE deleted = 0")
    }


def score_tags(
    neighbours: list[tuple[str, float]],
    note_tags: dict[str, list[str]],
    *,
    titles: dict[str, str],
    limit: int = 8,
) -> list[TagScore]:
    """Vote neighbour tags, divided by how common each tag is.

    Without the division, summed similarity rewards whichever tags happen to sit
    on the most neighbours — which is how `billing` won a question about
    key-encryption-key rotation in the measured run. The weight is an IDF over
    the neighbourhood: a tag carried by every neighbour says little about *this*
    note, and one carried by two close neighbours says a lot.
    """
    if not neighbours:
        return []
    raw: dict[str, float] = {}
    supporters: dict[str, int] = {}
    for note_id, similarity in neighbours:
        for tag_id in note_tags.get(note_id, []):
            raw[tag_id] = raw.get(tag_id, 0.0) + max(similarity, 0.0)
            supporters[tag_id] = supporters.get(tag_id, 0) + 1

    total = len(neighbours)
    scored = [
        TagScore(
            tag_id=tag_id,
            title=titles.get(tag_id, tag_id),
            # log(1 + N/df): flat for a tag on one neighbour, strongly damped for
            # one on all of them.
            score=weight * math.log(1 + total / supporters[tag_id]),
            supporters=supporters[tag_id],
        )
        for tag_id, weight in raw.items()
    ]
    scored.sort(key=lambda s: (-s.score, s.title))
    return scored[:limit]


def nearest_tagged(
    *,
    repo: SuggestionRepository,
    index_conn: sqlite3.Connection,
    vector: list[float],
    model: str,
    exclude_note_id: str | None = None,
    k: int = 25,
) -> list[tuple[str, float]]:
    """The k most similar *tagged* notes, by cosine similarity.

    A brute-force scan. At 2,240 notes and 1024 dimensions that is a few million
    multiplications — well under a second, and it avoids adding a vector index
    for a corpus this size.
    """
    candidates = tagged_notes(index_conn)
    scored: list[tuple[str, float]] = []
    for note_id in candidates:
        if note_id == exclude_note_id:
            continue
        other = repo.get_embedding_any_body(note_id, model=model)
        if other is None:
            continue
        scored.append((note_id, cosine(vector, other)))
    scored.sort(key=lambda pair: -pair[1])
    return scored[:k]
