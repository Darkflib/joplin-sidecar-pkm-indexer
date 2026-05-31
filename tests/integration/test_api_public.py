"""Public-endpoint tests: /health needs no auth (PRD §8.2, §18.1)."""

from fastapi.testclient import TestClient

from pkm_sidecar import __version__
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig


def test_health_no_auth(cfg: AppConfig) -> None:
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "service": "pkm-sidecar", "version": __version__}


def test_health_has_no_auth_requirement(cfg: AppConfig) -> None:
    # Even with a garbage Authorization header, /health is fine.
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get("/health", headers={"Authorization": "Bearer nonsense"})
    assert resp.status_code == 200
