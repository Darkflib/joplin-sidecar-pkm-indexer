"""In-memory fake Joplin Data API served via httpx.MockTransport.

Shared by unit and integration tests. Records every request so tests can assert
the read-only invariant (no non-GET request ever issued).
"""

from __future__ import annotations

from typing import Any

import httpx

from pkm_sidecar.joplin_client import JoplinClient


class FakeJoplin:
    def __init__(self, *, page_limit: int = 100, events_status: int = 200) -> None:
        self.notes: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.tags: dict[str, dict[str, Any]] = {}
        self.tag_notes: dict[str, set[str]] = {}
        self.events: list[dict[str, Any]] = []
        self._eid = 0
        self.page_limit = page_limit
        self.events_status = events_status
        self.requests: list[tuple[str, str]] = []

    # --- seeding -----------------------------------------------------------

    def add_folder(self, folder_id: str, title: str, **kw: Any) -> None:
        self.folders[folder_id] = {
            "id": folder_id,
            "parent_id": kw.get("parent_id", ""),
            "title": title,
            "created_time": kw.get("created_time", 0),
            "updated_time": kw.get("updated_time", 0),
        }

    def add_note(
        self, note_id: str, title: str = "T", body: str = "", parent_id: str = "", **kw: Any
    ) -> None:
        self.notes[note_id] = {
            "id": note_id,
            "parent_id": parent_id,
            "title": title,
            "body": body,
            "created_time": kw.get("created_time", 0),
            "updated_time": kw.get("updated_time", 0),
            "user_created_time": kw.get("user_created_time", 0),
            "user_updated_time": kw.get("user_updated_time", 0),
            "is_todo": kw.get("is_todo", 0),
            "todo_due": kw.get("todo_due"),
            "todo_completed": kw.get("todo_completed"),
            "source_url": kw.get("source_url"),
        }

    def add_tag(self, tag_id: str, title: str, note_ids: tuple[str, ...] = ()) -> None:
        self.tags[tag_id] = {"id": tag_id, "title": title, "created_time": 0, "updated_time": 0}
        self.tag_notes[tag_id] = set(note_ids)

    def push_event(self, item_type: int, item_id: str, change: int) -> None:
        self._eid += 1
        self.events.append(
            {"id": self._eid, "item_type": item_type, "item_id": item_id, "type": change}
        )

    # --- transport ---------------------------------------------------------

    def _page(self, items: list[dict[str, Any]], params: httpx.QueryParams) -> dict[str, Any]:
        page = int(params.get("page", "1"))
        limit = int(params.get("limit", str(self.page_limit)))
        start = (page - 1) * limit
        return {"items": items[start : start + limit], "has_more": start + limit < len(items)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method != "GET":
            return httpx.Response(405)
        path = request.url.path
        params = request.url.params
        if path == "/ping":
            return httpx.Response(200, text="JoplinClipperServer")
        if path == "/notes":
            return httpx.Response(200, json=self._page(list(self.notes.values()), params))
        if path.startswith("/notes/"):
            note = self.notes.get(path.split("/")[2])
            return httpx.Response(404, json={}) if note is None else httpx.Response(200, json=note)
        if path == "/folders":
            return httpx.Response(200, json=self._page(list(self.folders.values()), params))
        if path == "/tags":
            return httpx.Response(200, json=self._page(list(self.tags.values()), params))
        if path.startswith("/tags/") and path.endswith("/notes"):
            tid = path.split("/")[2]
            items = [{"id": nid} for nid in sorted(self.tag_notes.get(tid, set()))]
            return httpx.Response(200, json=self._page(items, params))
        if path == "/events":
            if self.events_status != 200:
                return httpx.Response(self.events_status, json={"error": "cursor too old"})
            cursor = params.get("cursor")
            after = int(cursor) if cursor not in (None, "") else 0
            items = [e for e in self.events if e["id"] > after]
            new_cursor = str(items[-1]["id"]) if items else (cursor or "0")
            return httpx.Response(
                200, json={"items": items, "cursor": new_cursor, "has_more": False}
            )
        return httpx.Response(404, json={})

    def client(self, **kw: Any) -> JoplinClient:
        return JoplinClient(
            "http://127.0.0.1:41184",
            "tok",
            transport=httpx.MockTransport(self.handler),
            page_limit=self.page_limit,
            max_retries=0,
            backoff_initial=0.0,
            **kw,
        )

    def only_get_requests(self) -> bool:
        return all(method == "GET" for method, _ in self.requests)
