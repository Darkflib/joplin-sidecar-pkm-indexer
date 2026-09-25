"""Embedding backfill, cosine, and tag scoring (docs/enrichment.md §6)."""

from pathlib import Path

import httpx
import pytest

from pkm_sidecar import db
from pkm_sidecar.config import AppConfig, load_config
from pkm_sidecar.enrichment import db as edb
from pkm_sidecar.enrichment.embeddings import (
    EMBED_CHARS,
    backfill_embeddings,
    cosine,
    embedding_text,
    score_tags,
)
from pkm_sidecar.enrichment.ollama_client import OllamaClient
from pkm_sidecar.enrichment.repository import SuggestionRepository


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text(
        '[enrichment]\nenabled = true\nembedding_model = "test-embed"\n'
    )
    return load_config(
        env={
            "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
            "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
        }
    )


@pytest.fixture
def index(cfg: AppConfig):
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    yield conn
    conn.close()


@pytest.fixture
def repo(tmp_path: Path):
    path = tmp_path / "suggestions.sqlite3"
    edb.init_db(path)
    conn = edb.open_connection(path)
    yield SuggestionRepository(conn)
    conn.close()


def _note(conn, note_id: str, title: str, body: str, *, body_hash: str | None = None) -> None:
    conn.execute(
        "INSERT INTO notes(id, title, body, body_hash, indexed_at, deleted, updated_time) "
        "VALUES (?, ?, ?, ?, 1, 0, 1)",
        (note_id, title, body, body_hash or f"hash-{note_id}"),
    )


def _embed_client(calls: list | None = None, dim: int = 4) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        n = len(payload["input"])
        if calls is not None:
            calls.append(n)
        return httpx.Response(200, json={"embeddings": [[0.1] * dim for _ in range(n)]})

    return OllamaClient(
        "http://127.0.0.1:11434", transport=httpx.MockTransport(handler), max_retries=0
    )


class TestEmbeddingText:
    def test_title_leads(self) -> None:
        assert embedding_text("The Title", "the body").startswith("The Title")

    def test_truncated(self) -> None:
        assert len(embedding_text("t", "x" * 5000)) == EMBED_CHARS


class TestCosine:
    def test_identical(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self) -> None:
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_does_not_divide_by_zero(self) -> None:
        assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestBackfill:
    async def test_embeds_and_caches(self, cfg, index, repo) -> None:
        with db.transaction(index):
            for i in range(5):
                _note(index, f"n{i}", f"Note {i}", "body text here")
        calls: list[int] = []
        async with _embed_client(calls) as client:
            first = await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
            second = await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
        assert (first.embedded, first.cached) == (5, 0)
        assert (second.embedded, second.cached) == (0, 5)
        assert calls == [5]  # one batch, and nothing on the second run

    async def test_batches_rather_than_one_call_per_note(self, cfg, index, repo) -> None:
        with db.transaction(index):
            for i in range(70):
                _note(index, f"n{i}", "T", "body")
        calls: list[int] = []
        async with _embed_client(calls) as client:
            await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
        assert calls == [32, 32, 6]

    async def test_changed_body_is_re_embedded(self, cfg, index, repo) -> None:
        with db.transaction(index):
            _note(index, "n1", "T", "original body text")
        async with _embed_client() as client:
            await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
            with db.transaction(index):
                index.execute("UPDATE notes SET body = 'a wholly different body' WHERE id = 'n1'")
            again = await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
        assert again.embedded == 1

    async def test_changed_title_is_re_embedded(self, cfg, index, repo) -> None:
        """The title leads the embedded text, so a retitle must invalidate it.

        Keying on notes.body_hash, which hashes the body alone, left a retitled
        note with a stale vector for ever while the backfill counted it current.
        """
        with db.transaction(index):
            _note(index, "n1", "Original Title", "body stays the same")
        async with _embed_client() as client:
            await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
            with db.transaction(index):
                index.execute("UPDATE notes SET title = 'A Completely New Title' WHERE id = 'n1'")
            again = await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
        assert again.embedded == 1
        assert again.cached == 0

    async def test_a_failed_batch_is_counted_not_fatal(self, cfg, index, repo) -> None:
        with db.transaction(index):
            for i in range(40):
                _note(index, f"n{i}", "T", "body")
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            n = len(json.loads(request.content)["input"])
            seen.append(n)
            if len(seen) == 1:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={"embeddings": [[0.1] * 4 for _ in range(n)]})

        client = OllamaClient(
            "http://127.0.0.1:11434", transport=httpx.MockTransport(handler), max_retries=0
        )
        async with client:
            result = await backfill_embeddings(index_conn=index, repo=repo, client=client, cfg=cfg)
        assert result.failed == 32
        assert result.embedded == 8  # the second batch still landed


TITLES = {"t1": "billing", "t2": "openbao", "t3": "security"}


class TestScoreTags:
    def test_no_neighbours_no_tags(self) -> None:
        assert score_tags([], {}, titles=TITLES) == []

    def test_a_tag_on_every_neighbour_does_not_win_on_volume(self) -> None:
        """Summing let a ubiquitous tag win; the mean asks the right question.

        Measured on the real vault: summing scored 64% top-1, exactly what a
        constant predictor scores. The mean scored 76%.
        """
        neighbours = [("a", 0.5), ("b", 0.5), ("c", 0.5), ("d", 0.95)]
        note_tags = {"a": ["t1"], "b": ["t1"], "c": ["t1"], "d": ["t2"]}
        scored = score_tags(neighbours, note_tags, titles=TITLES)
        # t1 sums to 1.5 across three weak neighbours; t2 is one close one.
        assert scored[0].tag_id == "t2"

    def test_score_is_the_mean_not_the_sum(self) -> None:
        scored = score_tags([("a", 0.8), ("b", 0.4)], {"a": ["t1"], "b": ["t1"]}, titles=TITLES)
        assert scored[0].score == pytest.approx(0.6)

    def test_frequency_damping_is_deliberately_absent(self) -> None:
        """IDF was measured and rejected: it cost 9 points of top-1 accuracy."""
        neighbours = [("a", 0.9), ("b", 0.9)]
        common = {"a": ["t1"], "b": ["t1"], **{f"x{i}": ["t1"] for i in range(40)}}
        scored = score_tags(neighbours, common, titles=TITLES)
        # t1 is on 42 corpus notes; its score is still just the mean similarity.
        assert scored[0].score == pytest.approx(0.9)

    def test_supporters_are_reported(self) -> None:
        scored = score_tags([("a", 0.9), ("b", 0.8)], {"a": ["t1"], "b": ["t1"]}, titles=TITLES)
        assert scored[0].supporters == 2

    def test_a_single_close_neighbour_scores_its_similarity(self) -> None:
        scored = score_tags([("a", 1.0), ("b", 1.0)], {"a": ["t1"]}, titles=TITLES)
        assert scored[0].score == pytest.approx(1.0)

    def test_limit_is_honoured(self) -> None:
        neighbours = [("a", 0.9)]
        note_tags = {"a": ["t1", "t2", "t3"]}
        assert len(score_tags(neighbours, note_tags, titles=TITLES, limit=2)) == 2

    def test_negative_similarity_does_not_subtract(self) -> None:
        scored = score_tags([("a", -0.5)], {"a": ["t1"]}, titles=TITLES)
        assert scored[0].score == 0.0
