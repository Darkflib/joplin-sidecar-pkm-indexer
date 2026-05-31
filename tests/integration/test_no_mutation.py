"""PRD §18.10: the sidecar must never issue a write/delete request to Joplin.

Exercises every flow that talks to Joplin (full rebuild, incremental sync,
single-note reindex) and asserts every intercepted request used GET. Complements
the source-level AST scan in tests/unit/test_joplin_client_readonly.py.
"""

import sqlite3

from pkm_sidecar import services
from pkm_sidecar.config import AppConfig
from pkm_sidecar.indexer import create_indexer
from pkm_sidecar.services import ITEM_TYPE_NOTE
from tests._fake_joplin import FakeJoplin


async def test_only_get_requests_across_all_flows(
    cfg: AppConfig, writer: sqlite3.Connection
) -> None:
    fake = FakeJoplin()
    fake.add_folder("f1", "Inbox")
    fake.add_note(
        "n1", "Note", "- [ ] task\n[ref](:/0123456789abcdef0123456789abcdef)", parent_id="f1"
    )
    fake.add_tag("t1", "project", note_ids=("n1",))

    async with fake.client() as client:
        await services.full_rebuild(writer, client, cfg)

        fake.notes["n1"]["body"] = "edited body"
        fake.push_event(ITEM_TYPE_NOTE, "n1", 2)
        await services.incremental_sync_once(writer, client, cfg)

        handle = create_indexer(cfg, writer, client)
        await handle.reindex_one("n1")

    assert fake.requests, "expected the flows to talk to Joplin"
    assert fake.only_get_requests()
    assert {method for method, _ in fake.requests} == {"GET"}
