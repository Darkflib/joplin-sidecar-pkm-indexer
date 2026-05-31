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


def test_static_css_served(client: TestClient) -> None:
    assert client.get("/static/dashboard.css").status_code == 200
