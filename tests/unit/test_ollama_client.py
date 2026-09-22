"""Enrichment model-host client (docs/enrichment.md §3)."""

import httpx
import pytest

from pkm_sidecar.enrichment.ollama_client import OllamaClient
from pkm_sidecar.errors import (
    OllamaBadResponseError,
    OllamaModelNotFoundError,
    OllamaUnreachableError,
    SecurityError,
)

BASE = "http://127.0.0.1:11434"


def _client(handler, **kw) -> OllamaClient:
    return OllamaClient(
        BASE, transport=httpx.MockTransport(handler), max_retries=0, backoff_initial=0.0, **kw
    )


class TestFence:
    async def test_other_origins_are_refused(self) -> None:
        """The Ollama fence is its own instance; it does not widen Joplin's."""
        inner = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        from pkm_sidecar.security import SingleOriginTransport

        transport = SingleOriginTransport(BASE, inner)
        with pytest.raises(SecurityError):
            await transport.handle_async_request(httpx.Request("POST", "https://evil.example/x"))

    async def test_joplin_origin_is_refused_by_the_ollama_fence(self) -> None:
        inner = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        from pkm_sidecar.security import SingleOriginTransport

        transport = SingleOriginTransport(BASE, inner)
        with pytest.raises(SecurityError):
            await transport.handle_async_request(
                httpx.Request("GET", "http://127.0.0.1:41184/notes")
            )


class TestPingAndModels:
    async def test_ping_true(self) -> None:
        async with _client(lambda r: httpx.Response(200, json={"models": []})) as c:
            assert await c.ping() is True

    async def test_ping_false_when_unreachable_and_never_raises(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(boom) as c:
            assert await c.ping() is False

    async def test_list_models(self) -> None:
        payload = {"models": [{"name": "llama3.1:8b"}, {"name": "bge-large:335m-en-v1.5-fp16"}]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            assert await c.list_models() == ["llama3.1:8b", "bge-large:335m-en-v1.5-fp16"]

    async def test_list_models_unreachable_raises(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(boom) as c:
            with pytest.raises(OllamaUnreachableError):
                await c.list_models()


class TestGenerate:
    async def test_returns_the_response(self) -> None:
        payload = {"response": "  Install Fail2ban on CentOS  ", "done_reason": "stop"}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            result = await c.generate("llama3.1:8b", "prompt")
        assert result.response.strip() == "Install Fail2ban on CentOS"
        assert result.spent_budget_thinking is False

    async def test_temperature_defaults_to_zero(self) -> None:
        """A 1.5B model was measured contradicting itself between runs at 0.2."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"response": "x"})

        async with _client(handler) as c:
            await c.generate("m", "p")
        assert seen["options"]["temperature"] == 0.0
        assert seen["stream"] is False

    async def test_reasoning_model_budget_trap_is_detected(self) -> None:
        """gpt-oss:20b really does return an empty answer with a full thinking."""
        payload = {"response": "", "thinking": "We need a short title...", "done_reason": "length"}
        async with _client(payload and (lambda r: httpx.Response(200, json=payload))) as c:
            result = await c.generate("gpt-oss:20b", "p", num_predict=24)
        assert result.response == ""
        assert result.spent_budget_thinking is True
        assert result.truncated is True

    async def test_thinking_is_not_mistaken_for_the_answer(self) -> None:
        payload = {"response": "The Real Title", "thinking": "some working"}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            result = await c.generate("m", "p")
        assert result.response == "The Real Title"
        assert result.spent_budget_thinking is False

    async def test_missing_model_names_the_pull_command(self) -> None:
        async with _client(lambda r: httpx.Response(404, json={"error": "model not found"})) as c:
            with pytest.raises(OllamaModelNotFoundError) as ei:
                await c.generate("nope:7b", "p")
        assert "ollama pull nope:7b" in str(ei.value)

    async def test_non_json_body_is_a_bad_response(self) -> None:
        async with _client(lambda r: httpx.Response(200, text="<html>nope</html>")) as c:
            with pytest.raises(OllamaBadResponseError):
                await c.generate("m", "p")

    async def test_unreachable_host(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(boom) as c:
            with pytest.raises(OllamaUnreachableError):
                await c.generate("m", "p")


class TestEmbed:
    async def test_roundtrip(self) -> None:
        payload = {"embeddings": [[0.5, -0.25], [1.0, 0.0]]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            out = await c.embed("bge", ["a", "b"])
        assert out == [[0.5, -0.25], [1.0, 0.0]]

    async def test_empty_input_short_circuits(self) -> None:
        def never(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not have called the host")

        async with _client(never) as c:
            assert await c.embed("bge", []) == []

    async def test_count_mismatch_is_rejected(self) -> None:
        """Silently returning fewer vectors than inputs would misalign every kNN."""
        payload = {"embeddings": [[0.1, 0.2]]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            with pytest.raises(OllamaBadResponseError) as ei:
                await c.embed("bge", ["a", "b", "c"])
        assert "Expected 3" in str(ei.value)

    async def test_missing_embeddings_key_is_rejected(self) -> None:
        async with _client(lambda r: httpx.Response(200, json={})) as c:
            with pytest.raises(OllamaBadResponseError):
                await c.embed("bge", ["a"])


class TestProbeRobustness:
    """`doctor` is interactive and catches only OllamaError — both matter here."""

    async def test_probes_do_not_inherit_the_generation_timeout(self) -> None:
        """A host that accepts then stalls must not hang doctor for two minutes."""
        seen: list[float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            t = request.extensions.get("timeout") or {}
            seen.append(t.get("read"))
            return httpx.Response(200, json={"models": []})

        async with _client(handler) as c:
            await c.ping()
            await c.list_models()

        from pkm_sidecar.enrichment.ollama_client import DEFAULT_TIMEOUT, PROBE_TIMEOUT

        assert seen == [PROBE_TIMEOUT, PROBE_TIMEOUT]
        assert PROBE_TIMEOUT < DEFAULT_TIMEOUT

    async def test_html_model_list_becomes_an_ollama_error(self) -> None:
        """Base URL pointing at a web server or proxy: a 200 full of HTML."""
        async with _client(lambda r: httpx.Response(200, text="<html>hello</html>")) as c:
            with pytest.raises(OllamaBadResponseError) as ei:
                await c.list_models()
        assert "non-JSON" in str(ei.value)

    async def test_non_object_json_becomes_an_ollama_error(self) -> None:
        async with _client(lambda r: httpx.Response(200, json=["not", "an", "object"])) as c:
            with pytest.raises(OllamaBadResponseError):
                await c.list_models()

    async def test_models_not_a_list_becomes_an_ollama_error(self) -> None:
        async with _client(lambda r: httpx.Response(200, json={"models": "nope"})) as c:
            with pytest.raises(OllamaBadResponseError):
                await c.list_models()

    async def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        payload = {"models": [{"name": "llama3.1:8b"}, "junk", {"no_name": 1}]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            assert await c.list_models() == ["llama3.1:8b"]

    async def test_doctor_survives_a_host_that_is_not_ollama(self, tmp_path) -> None:
        """The whole point: one failed check, not an aborted run."""
        from pkm_sidecar.errors import OllamaError

        async with _client(lambda r: httpx.Response(200, text="<html/>")) as c:
            with pytest.raises(OllamaError):  # doctor catches this and reports it
                await c.list_models()


class TestMalformedShapes:
    """Every failure from this host must arrive as an OllamaError, not a TypeError."""

    async def test_post_rejects_a_non_object_root(self) -> None:
        """A JSON array root would reach .get() and raise AttributeError."""
        async with _client(lambda r: httpx.Response(200, json=["not", "an", "object"])) as c:
            with pytest.raises(OllamaBadResponseError):
                await c.generate("m", "p")

    async def test_post_rejects_a_bare_string_root(self) -> None:
        async with _client(lambda r: httpx.Response(200, json="just a string")) as c:
            with pytest.raises(OllamaBadResponseError):
                await c.embed("m", ["a"])

    async def test_non_string_model_names_are_dropped(self) -> None:
        """doctor builds a set() from this; an unhashable name would raise TypeError."""
        payload = {"models": [{"name": "good:8b"}, {"name": ["a", "list"]}, {"name": {"k": 1}}]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            names = await c.list_models()
        assert names == ["good:8b"]
        assert set(names)  # the thing doctor actually does

    async def test_embedding_that_is_not_a_list_is_rejected(self) -> None:
        payload = {"embeddings": [[0.1, 0.2], "not-a-vector"]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            with pytest.raises(OllamaBadResponseError) as ei:
                await c.embed("bge", ["a", "b"])
        assert "expected a list" in str(ei.value).lower()

    async def test_embedding_with_a_non_numeric_element_is_rejected(self) -> None:
        payload = {"embeddings": [[0.1, "banana"]]}
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            with pytest.raises(OllamaBadResponseError) as ei:
                await c.embed("bge", ["a"])
        assert "non-numeric" in str(ei.value)


class TestUrlScrubbing:
    def test_error_url_is_scrubbed(self) -> None:
        from pkm_sidecar.errors import OllamaError

        err = OllamaError("boom", url="https://user:pw@host:11434/api/tags?api_key=secret")
        assert "pw" not in err.url
        assert "secret" not in err.url
        assert "host:11434" in err.url

    def test_scrub_url_cases(self) -> None:
        from pkm_sidecar.errors import scrub_url

        assert scrub_url("http://192.168.16.62:11434") == "http://192.168.16.62:11434"
        assert scrub_url("https://u:p@h/x") == "https://***@h/x"
        assert "***" in scrub_url("http://h/v1?key=abc123")
        assert "abc123" not in scrub_url("http://h/v1?key=abc123")
