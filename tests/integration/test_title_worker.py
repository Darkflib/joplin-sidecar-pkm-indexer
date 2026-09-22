"""The title worker end to end: route, generate, store, cache (docs/enrichment.md §7)."""

from pathlib import Path

import httpx
import pytest

from pkm_sidecar import db
from pkm_sidecar.config import AppConfig, load_config
from pkm_sidecar.enrichment import db as edb
from pkm_sidecar.enrichment.ollama_client import OllamaClient
from pkm_sidecar.enrichment.repository import SuggestionRepository
from pkm_sidecar.enrichment.worker import SLUG_SOURCE, generate_titles

PROSE = (
    "RabbitMQ consumer acks before the ledger write commits, so a crash between "
    "the two replays the delta and double counts the spend for that app."
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text(
        '[enrichment]\nenabled = true\ntitle_model = "test-model:1b"\n'
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


def _note(conn, note_id: str, title: str, body: str, *, updated: int = 100) -> None:
    conn.execute(
        "INSERT INTO notes(id, title, body, body_hash, indexed_at, deleted, updated_time) "
        "VALUES (?, ?, ?, ?, 1, 0, ?)",
        (note_id, title, body, f"hash-{note_id}", updated),
    )


def _client(answer: str = "A Perfectly Good Title", calls: list | None = None) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, json={"response": answer})

    return OllamaClient(
        "http://127.0.0.1:11434", transport=httpx.MockTransport(handler), max_retries=0
    )


class TestRouting:
    async def test_prose_note_goes_to_the_model(self, cfg, index, repo) -> None:
        with db.transaction(index):
            _note(index, "n1", "", PROSE)
        async with _client() as client:
            result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
        assert (result.from_model, result.from_slug) == (1, 0)
        stored = repo.current_for_note("n1", "title")
        assert stored.payload == {"title": "A Perfectly Good Title"}
        assert stored.model == "test-model:1b"
        assert stored.current_value == {"title": ""}
        assert stored.reason == "empty"

    async def test_bookmark_uses_the_url_and_never_calls_the_model(self, cfg, index, repo) -> None:
        """The slug path is the only one that can work on a dead link."""
        url = "https://www.tecmint.com/free-linux-shell-scripting-books/"
        with db.transaction(index):
            _note(index, "n1", url, url)
        calls: list[str] = []
        async with _client(calls=calls) as client:
            result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)

        assert (result.from_slug, result.from_model) == (1, 0)
        assert calls == []  # no model call at all
        stored = repo.current_for_note("n1", "title")
        assert stored.payload == {"title": "Free Linux Shell Scripting Books"}
        assert stored.model == SLUG_SOURCE
        assert stored.confidence == pytest.approx(0.9)

    async def test_slug_path_works_with_no_client_configured(self, cfg, index, repo) -> None:
        url = "https://www.tecmint.com/free-linux-shell-scripting-books/"
        with db.transaction(index):
            _note(index, "n1", url, url)
        result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=None)
        assert result.from_slug == 1
        assert result.failed == 0

    async def test_note_with_nothing_to_work_from_is_skipped(self, cfg, index, repo) -> None:
        """A screenshot body gives a model nothing; it would invent something."""
        calls: list[str] = []
        with db.transaction(index):
            _note(index, "n1", "Screenshot_20250426.png", "![x](:/" + "a" * 32 + ")")
        async with _client(calls=calls) as client:
            result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
        assert result.nothing_to_work_from == 1
        assert result.stored == 0
        assert calls == []


class TestCachingAndGuards:
    async def test_second_run_spends_nothing(self, cfg, index, repo) -> None:
        with db.transaction(index):
            _note(index, "n1", "", PROSE)
        calls: list[str] = []
        async with _client(calls=calls) as client:
            await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
            assert len(calls) == 1
            again = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
        assert again.already_suggested == 1
        assert again.stored == 0
        assert len(calls) == 1  # unchanged: no second model call

    async def test_opted_out_notes_are_left_alone(self, cfg, index, repo) -> None:
        with db.transaction(index):
            _note(index, "n1", "", PROSE)
        with repo.transaction():
            repo.opt_out("n1", "title")
        calls: list[str] = []
        async with _client(calls=calls) as client:
            result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
        assert result.opted_out == 1
        assert calls == []

    async def test_unusable_model_output_is_rejected_not_stored(self, cfg, index, repo) -> None:
        with db.transaction(index):
            _note(index, "n1", "", PROSE)
        async with _client(answer="   ") as client:
            result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
        assert result.rejected == 1
        assert repo.current_for_note("n1", "title") is None

    async def test_model_failure_is_counted_not_fatal(self, cfg, index, repo) -> None:
        """One bad note must not abandon the rest of an overnight batch."""
        with db.transaction(index):
            _note(index, "n1", "", PROSE, updated=200)
            _note(index, "n2", "", PROSE, updated=100)

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append("call")
            if len(seen) == 1:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={"response": "Second Note Title"})

        client = OllamaClient(
            "http://127.0.0.1:11434", transport=httpx.MockTransport(handler), max_retries=0
        )
        async with client:
            result = await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)

        assert result.failed == 1
        assert result.from_model == 1  # the other note still got done
        assert repo.current_for_note("n2", "title") is not None

    async def test_dry_run_generates_but_stores_nothing(self, cfg, index, repo) -> None:
        with db.transaction(index):
            _note(index, "n1", "", PROSE)
        async with _client() as client:
            result = await generate_titles(
                index_conn=index, repo=repo, cfg=cfg, client=client, dry_run=True
            )
        assert result.from_model == 1
        assert repo.current_for_note("n1", "title") is None

    async def test_apply_metadata_is_carried_for_the_write_back_increment(
        self, cfg, index, repo
    ) -> None:
        with db.transaction(index):
            _note(index, "n1", "", PROSE, updated=4242)
        async with _client() as client:
            await generate_titles(index_conn=index, repo=repo, cfg=cfg, client=client)
        stored = repo.current_for_note("n1", "title")
        assert stored.note_updated_time == 4242  # optimistic-concurrency token
        assert stored.note_body_hash == "hash-n1"
