"""Read-only invariant tests for the Joplin client (PRD §1051, §8.4)."""

import ast
from pathlib import Path

import httpx
import pytest

from pkm_sidecar import joplin_client
from pkm_sidecar.errors import JoplinBadResponseError, SecurityError

CLIENT_SOURCE = Path(joplin_client.__file__)
_MUTATING_METHODS = {"post", "put", "patch", "delete", "send", "stream"}


def test_no_mutating_httpx_calls_in_source() -> None:
    """AST-scan the client: it must never call a non-GET httpx method."""
    tree = ast.parse(CLIENT_SOURCE.read_text())
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATING_METHODS
    ]
    assert offenders == [], f"client uses non-GET httpx method(s): {offenders}"


def test_only_get_methods_are_public() -> None:
    public = {n for n in dir(joplin_client.JoplinClient) if not n.startswith("_")}
    expected = {
        "aclose",
        "ping",
        "get_note",
        "get_notes",
        "get_folders",
        "get_tags",
        "get_tag_notes",
        "get_note_tags",
        "get_events",
    }
    assert public == expected, f"unexpected public surface: {public ^ expected}"


async def test_disallowed_path_is_refused() -> None:
    client = joplin_client.JoplinClient("http://127.0.0.1:41184", "tok")
    with pytest.raises(JoplinBadResponseError):
        await client._request("/resources/abc")  # not in ALLOWED_PATHS
    await client.aclose()


async def test_transport_is_local_only() -> None:
    # The client wraps LocalOnlyTransport; a foreign URL raises SecurityError.
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    transport = joplin_client.security.LocalOnlyTransport("http://127.0.0.1:41184", inner)
    with pytest.raises(SecurityError):
        await transport.handle_async_request(httpx.Request("GET", "https://evil.example/x"))
