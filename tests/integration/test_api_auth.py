"""Auth-gating + read-only-surface tests for the API (PRD §8.2, §8.4)."""

import pytest
from fastapi.testclient import TestClient

from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig, load_config
from pkm_sidecar.errors import SecurityError
from pkm_sidecar.security import ephemeral_token_file_path

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


class TestNonLocalBindGuard:
    """A bind outside loopback must carry a token the operator chose (PRD §8.1).

    The README has always promised this; the check existed but was never called,
    so `serve --host 0.0.0.0 --allow-non-localhost` with no PKM_SIDECAR_API_TOKEN
    started happily behind an auto-generated token nobody had seen.
    """

    @staticmethod
    def _non_local_cfg(tmp_path, monkeypatch, **extra: str) -> AppConfig:
        monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "rt"))
        (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cfg" / "config.toml").write_text("")
        return load_config(
            env={
                "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
                "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
                "PKM_SIDECAR_HOST": "0.0.0.0",
                **extra,
            },
            cli_overrides={"allow_non_localhost": True},
        )

    def test_startup_refused_without_a_configured_token(self, tmp_path, monkeypatch) -> None:
        cfg = self._non_local_cfg(tmp_path, monkeypatch)
        with pytest.raises(SecurityError), TestClient(create_app(cfg, start_indexer_loop=False)):
            pass  # pragma: no cover - lifespan raises before the body runs

    def test_no_token_file_left_behind_by_a_refused_start(self, tmp_path, monkeypatch) -> None:
        cfg = self._non_local_cfg(tmp_path, monkeypatch)
        with pytest.raises(SecurityError), TestClient(create_app(cfg, start_indexer_loop=False)):
            pass  # pragma: no cover
        assert not ephemeral_token_file_path(cfg).exists()

    def test_startup_allowed_with_a_configured_token(self, tmp_path, monkeypatch) -> None:
        cfg = self._non_local_cfg(tmp_path, monkeypatch, PKM_SIDECAR_API_TOKEN="chosen")
        with TestClient(create_app(cfg, start_indexer_loop=False)) as c:
            assert c.get("/health").status_code == 200

    def test_loopback_start_is_unaffected(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200
