"""Auth-gating + read-only-surface tests for the API (PRD §8.2, §8.4)."""

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig

AUTH = {"Authorization": "Bearer test-api-token"}

PROTECTED_GETS = [
    "/api/status",
    "/api/notes/recent",
    "/api/notes/inbox",
    "/api/notes/untagged",
    "/api/notes/todos",
    "/api/notes/stale",
    "/api/search?q=hello",
    "/api/note/abc",
    "/api/note/abc/tasks",
    "/api/note/abc/links",
]


@pytest.fixture
def client(cfg: AppConfig):
    with TestClient(create_app(cfg, start_indexer_loop=False)) as c:
        yield c


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_get_requires_auth(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize("path", ["/api/index/rebuild", "/api/index/sync", "/api/index/note/abc"])
def test_post_requires_auth(client: TestClient, path: str) -> None:
    assert client.post(path).status_code == 401


def test_list_views_ok_with_auth(client: TestClient) -> None:
    for path in [
        "/api/notes/recent",
        "/api/notes/untagged",
        "/api/notes/todos",
        "/api/notes/stale",
    ]:
        resp = client.get(path, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == []  # empty index


def test_openapi_exposes_no_mutating_methods(cfg: AppConfig) -> None:
    schema = create_app(cfg, start_indexer_loop=False).openapi()
    methods: set[str] = set()
    posts: list[str] = []
    for path, ops in schema["paths"].items():
        for method in ops:
            methods.add(method.upper())
            if method.upper() == "POST":
                posts.append(path)
    assert "PUT" not in methods
    assert "PATCH" not in methods
    assert "DELETE" not in methods
    assert sorted(posts) == ["/api/index/note/{note_id}", "/api/index/rebuild", "/api/index/sync"]
