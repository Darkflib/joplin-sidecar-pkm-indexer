"""Pagination, error mapping, and token-safety tests for the Joplin client."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from pkm_sidecar import logging_config
from pkm_sidecar.errors import (
    JoplinAuthError,
    JoplinCursorInvalidError,
    JoplinNotFoundError,
    JoplinUnreachableError,
)
from pkm_sidecar.joplin_client import JoplinClient

FIXTURES = Path(__file__).parent.parent / "fixtures" / "joplin"
BASE = "http://127.0.0.1:41184"
TOKEN = "joplin-secret-token-abc123xyz"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _clean_logging():
    logging_config.reset_for_tests()
    yield
    logging_config.reset_for_tests()


def _client(handler, **kw) -> JoplinClient:
    return JoplinClient(
        BASE,
        TOKEN,
        transport=httpx.MockTransport(handler),
        max_retries=1,
        backoff_initial=0.0,
        **kw,
    )


class TestPagination:
    async def test_follows_pages(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page")
            body = (
                _fixture("notes_page1.json")
                if page in (None, "1")
                else _fixture("notes_page2.json")
            )
            return httpx.Response(200, json=body)

        client = _client(handler)
        ids = [n["id"] async for n in client.get_notes(fields=["id", "title", "body"])]
        await client.aclose()
        assert ids == ["a1", "a2", "a3"]

    async def test_limit_stops_early(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page")
            body = (
                _fixture("notes_page1.json")
                if page in (None, "1")
                else _fixture("notes_page2.json")
            )
            return httpx.Response(200, json=body)

        client = _client(handler)
        ids = [n["id"] async for n in client.get_notes(fields=["id"], limit=2)]
        await client.aclose()
        assert ids == ["a1", "a2"]

    async def test_token_passed_as_query(self) -> None:
        seen: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["token"] = request.url.params.get("token")
            return httpx.Response(200, json=_fixture("folders.json"))

        client = _client(handler)
        _ = [f async for f in client.get_folders(fields=["id", "title"])]
        await client.aclose()
        assert seen["token"] == TOKEN


class TestSingleAndEvents:
    async def test_get_note(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "a1", "title": "First", "body": "x"})

        client = _client(handler)
        note = await client.get_note("a1", fields=["id", "title", "body"])
        await client.aclose()
        assert note["id"] == "a1"

    async def test_get_events(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_fixture("events_page.json"))

        client = _client(handler)
        result = await client.get_events(cursor="0")
        await client.aclose()
        assert result["cursor"] == "42"
        assert result["has_more"] is False
        assert len(result["items"]) == 1

    async def test_ping_true(self) -> None:
        client = _client(lambda r: httpx.Response(200, text="JoplinClipperServer"))
        assert await client.ping() is True
        await client.aclose()

    async def test_ping_false_on_error(self) -> None:
        client = _client(lambda r: httpx.Response(500))
        assert await client.ping() is False
        await client.aclose()


class TestErrorMapping:
    async def test_404(self) -> None:
        client = _client(lambda r: httpx.Response(404))
        with pytest.raises(JoplinNotFoundError):
            await client.get_note("missing", fields=["id"])
        await client.aclose()

    async def test_401_auth(self) -> None:
        client = _client(lambda r: httpx.Response(401))
        with pytest.raises(JoplinAuthError):
            await client.get_note("a1", fields=["id"])
        await client.aclose()

    async def test_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _client(handler)
        with pytest.raises(JoplinUnreachableError):
            await client.get_note("a1", fields=["id"])
        await client.aclose()

    async def test_events_400_is_cursor_invalid(self) -> None:
        client = _client(lambda r: httpx.Response(400, json={"error": "cursor too old"}))
        with pytest.raises(JoplinCursorInvalidError):
            await client.get_events(cursor="ancient")
        await client.aclose()


class TestTokenSafety:
    async def test_token_never_in_exception_repr(self) -> None:
        client = _client(lambda r: httpx.Response(401))
        try:
            await client.get_note("a1", fields=["id"])
        except JoplinAuthError as exc:
            assert TOKEN not in repr(exc)
            assert TOKEN not in str(exc.url or "")
        await client.aclose()

    async def test_token_never_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        logging_config.configure_logging("DEBUG", redact_values=[TOKEN])
        client = _client(lambda r: httpx.Response(500))
        with caplog.at_level(logging.DEBUG):
            await client.ping()
        await client.aclose()
        assert TOKEN not in caplog.text
