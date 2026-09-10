"""Tests for the dashboard shell and assets (PRD §14)."""

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar import __version__
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig


@pytest.fixture
def client(cfg: AppConfig):
    with TestClient(create_app(cfg, start_indexer_loop=False)) as c:
        yield c


def test_index_is_public_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_index_has_no_token_literals(client: TestClient) -> None:
    body = client.get("/").text
    # The sidecar API token (test-api-token) must never be embedded in the HTML.
    assert "test-api-token" not in body
    assert "Bearer" not in body  # auth is attached by JS, not server-rendered


def test_index_csp_header(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.headers["Content-Security-Policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
    )


def test_asset_urls_are_version_stamped(client: TestClient) -> None:
    body = client.get("/").text
    assert f"/static/dashboard.js?v={__version__}" in body
    assert f"/static/dashboard.css?v={__version__}" in body


def test_static_js_served_and_token_safe(client: TestClient) -> None:
    resp = client.get("/static/dashboard.js")
    assert resp.status_code == 200
    js = resp.text
    # Implements the fragment handoff contract.
    assert "sessionStorage" in js
    assert "token=" in js
    assert "replaceState" in js
    assert "localStorage" not in js  # never persist the token to localStorage
    assert "test-api-token" not in js


def test_last_sync_uses_newest_of_both_index_stamps(client: TestClient) -> None:
    """A full rebuild after incrementals must not display the stale incremental time.

    services.py writes META_LAST_FULL_INDEX_AT and META_LAST_INCREMENTAL_INDEX_AT
    independently — neither updates the other — so picking the incremental stamp
    whenever it is set shows the wrong time after a rebuild.

    There is no JS runtime in CI, so this asserts the whole assignment chain
    rather than a substring: both stamps must feed `stamps`, `lastSecs` must come
    from `Math.max` over it, and `lastMs` must derive from `lastSecs`. That way
    the test cannot pass while `lastMs` is still taken from one stamp directly.
    """
    js = client.get("/static/dashboard.js").text
    compact = " ".join(js.split())

    # Both stamps are candidates, not one preferred over the other.
    assert (
        "const stamps = [ s.indexing.last_incremental_index_at, "
        "s.indexing.last_full_index_at, ]" in compact
    )
    # The newest of them wins, and the displayed value derives from that choice.
    assert "const lastSecs = stamps.length ? Math.max(...stamps) : null;" in compact
    assert "const lastMs = lastSecs ? lastSecs * 1000 : null;" in compact

    # The old `a || b` form silently preferred the incremental stamp.
    assert "s.indexing.last_incremental_index_at || s.indexing.last_full_index_at" not in compact


def test_static_css_served(client: TestClient) -> None:
    assert client.get("/static/dashboard.css").status_code == 200
