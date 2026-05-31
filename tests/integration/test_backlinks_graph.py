"""Tests for backlinks and the note-link graph (PRD v0.2)."""

import asyncio
import sqlite3

from fastapi.testclient import TestClient

from pkm_sidecar import db, services
from pkm_sidecar.app import create_app
from pkm_sidecar.config import AppConfig
from pkm_sidecar.models import ExtractedLink
from pkm_sidecar.repositories import NoteRepository
from tests._fake_joplin import FakeJoplin

AUTH = {"Authorization": "Bearer test-api-token"}

# 32-hex ids so the links classify as internal_joplin.
A = "a" * 32
B = "b" * 32
C = "c" * 32


async def _seed(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    fake = FakeJoplin()
    # A -> B, C -> B (two backlinks to B); B -> A.
    fake.add_note(A, "Note A", f"links to [B](:/{B})")
    fake.add_note(B, "Note B", f"links back to [A](:/{A})")
    fake.add_note(C, "Note C", f"also references [B](:/{B})")
    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)


async def test_backlinks(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    await _seed(cfg, writer)
    repo = NoteRepository(writer)
    backlinks = {n.id for n in repo.get_backlinks(B)}
    assert backlinks == {A, C}  # both A and C link to B
    assert {n.id for n in repo.get_backlinks(A)} == {B}  # only B links to A


async def test_graph(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    await _seed(cfg, writer)
    repo = NoteRepository(writer)
    nodes, edges, truncated = repo.get_graph()
    assert truncated is False
    edge_pairs = {(e.source, e.target) for e in edges}
    assert edge_pairs == {(A, B), (C, B), (B, A)}
    assert {n.id for n in nodes} == {A, B, C}


def test_graph_truncation(cfg: AppConfig, writer: sqlite3.Connection) -> None:
    repo = NoteRepository(writer)
    with repo.transaction():
        for i in (A, B, C):
            repo.upsert_note({"id": i, "title": i, "body": ""}, indexed_at=1)
        repo.replace_links_for_note(
            A,
            [
                ExtractedLink(note_id=A, target=f":/{B}", link_type="internal_joplin"),
                ExtractedLink(note_id=A, target=f":/{C}", link_type="internal_joplin"),
            ],
            indexed_at=1,
        )
    _, edges, truncated = repo.get_graph(limit=1)
    assert len(edges) == 1
    assert truncated is True


def test_backlinks_and_graph_endpoints(cfg: AppConfig) -> None:
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    asyncio.run(_seed(cfg, conn))
    conn.close()
    with TestClient(create_app(cfg, start_indexer_loop=False)) as client:
        resp = client.get(f"/api/note/{B}/backlinks", headers=AUTH)
        assert resp.status_code == 200
        assert {n["id"] for n in resp.json()} == {A, C}
        g = client.get("/api/graph", headers=AUTH).json()
        assert g["truncated"] is False
        assert len(g["edges"]) == 3
