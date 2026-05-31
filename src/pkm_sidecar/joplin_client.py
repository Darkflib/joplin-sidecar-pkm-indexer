"""Read-only async Joplin Data API client (PRD §10).

**Read-only is structural**: this module exposes only ``get_*``/``ping`` methods
and only ever calls ``self._client.get``. There is no code path that issues a
POST/PUT/PATCH/DELETE — a unit test AST-scans this file to keep it that way. As a
second layer, every request goes through :class:`security.LocalOnlyTransport`, so
a request to any host other than the configured Joplin base raises at runtime.

The Joplin token is passed as a ``token`` query parameter; it is never logged
(httpx is pinned to WARNING and the redaction filter scrubs it), and any URL
stored on a raised exception is token-scrubbed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any

import httpx

from pkm_sidecar import security
from pkm_sidecar.errors import (
    JoplinAuthError,
    JoplinBadResponseError,
    JoplinCursorInvalidError,
    JoplinError,
    JoplinNotFoundError,
    JoplinRateLimitedError,
    JoplinUnreachableError,
    scrub_token,
)
from pkm_sidecar.logging_config import LogOnceGuard, get_logger, retry_with_backoff

# Query parameter values we send to Joplin are always strings or ints.
QueryValue = str | int

# Re-exported for the logging subsystem / callers that want URL scrubbing.
redact_token = scrub_token

# First path segment must be one of these (defence in depth against drift).
ALLOWED_PATHS: frozenset[str] = frozenset({"ping", "notes", "folders", "tags", "events"})

# httpx errors worth a conservative retry (transient transport/connection issues).
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

_MAX_PAGES = 10_000


class JoplinClient:
    """Async, GET-only client for the local Joplin Data API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        event_poll_timeout: float = 5.0,
        page_limit: int = 100,
        max_retries: int = 2,
        backoff_initial: float = 0.5,
        user_agent: str = "pkm-sidecar/0.1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._page_limit = page_limit
        self._max_retries = max_retries
        self._backoff_initial = backoff_initial
        self._event_poll_timeout = event_poll_timeout
        self._log = get_logger("joplin_client")
        self._ping_guard = LogOnceGuard()
        # Wrap the (real or test) transport so non-Joplin URLs are refused.
        guarded = security.LocalOnlyTransport(self._base_url, transport)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={"User-Agent": user_agent},
            transport=guarded,
        )

    # --- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> JoplinClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- internal request helper ------------------------------------------

    def _display_url(self, path: str) -> str:
        """A token-free URL for error messages."""
        return f"{self._base_url}{path}"

    @staticmethod
    def _assert_allowed(path: str) -> None:
        segment = path.lstrip("/").split("/", 1)[0]
        if segment not in ALLOWED_PATHS:
            raise JoplinBadResponseError(f"Refusing request to disallowed path: {path}")

    def _check_status(self, resp: httpx.Response, path: str, *, cursor_path: bool) -> None:
        code = resp.status_code
        if 200 <= code < 300:
            return
        url = scrub_token(str(resp.request.url))
        if code in (401, 403):
            raise JoplinAuthError("Joplin rejected the token.", url=url, status_code=code)
        if code == 404:
            raise JoplinNotFoundError("Joplin resource not found.", url=url, status_code=code)
        if code == 429:
            raise JoplinRateLimitedError(
                "Joplin rate limited the request.", url=url, status_code=code
            )
        if cursor_path and code == 400:
            raise JoplinCursorInvalidError(
                "Joplin event cursor is invalid.", url=url, status_code=code
            )
        raise JoplinBadResponseError(f"Unexpected Joplin status {code}.", url=url, status_code=code)

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, QueryValue] | None = None,
        timeout: httpx.Timeout | None = None,
        cursor_path: bool = False,
    ) -> httpx.Response:
        self._assert_allowed(path)
        query: dict[str, QueryValue] = dict(params or {})
        query["token"] = self._token

        async def _do() -> httpx.Response:
            return await self._client.get(
                path,
                params=query,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )

        try:
            resp = await retry_with_backoff(
                _do,
                retries=self._max_retries,
                base_delay=self._backoff_initial,
                retry_on=_TRANSIENT_ERRORS,
                logger=self._log,
                op=f"GET {path}",
            )
        except _TRANSIENT_ERRORS as exc:
            raise JoplinUnreachableError(
                f"Could not reach Joplin: {type(exc).__name__}", url=self._display_url(path)
            ) from exc
        self._check_status(resp, path, cursor_path=cursor_path)
        return resp

    async def _paginate(
        self, path: str, base_params: dict[str, QueryValue], limit: int | None
    ) -> AsyncIterator[dict[str, Any]]:
        yielded = 0
        for page in range(1, _MAX_PAGES + 1):
            params: dict[str, QueryValue] = dict(base_params)
            params["limit"] = self._page_limit
            params["page"] = page
            resp = await self._request(path, params=params)
            data: dict[str, Any] = resp.json()
            for item in data.get("items", []):
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not data.get("has_more"):
                return

    # --- public read methods ----------------------------------------------

    async def ping(self) -> bool:
        """GET /ping; return True on success, False on any error (never raises)."""
        try:
            resp = await self._request("/ping")
            return resp.status_code == 200
        except JoplinError as exc:
            key = (
                "joplin.unreachable" if isinstance(exc, JoplinUnreachableError) else "joplin.error"
            )
            if self._ping_guard.should_log(key):
                self._log.warning("Joplin ping failed: %s", type(exc).__name__)
            return False

    async def get_note(self, note_id: str, *, fields: Sequence[str]) -> dict[str, Any]:
        resp = await self._request(f"/notes/{note_id}", params={"fields": ",".join(fields)})
        data: dict[str, Any] = resp.json()
        return data

    def get_notes(
        self,
        *,
        fields: Sequence[str],
        limit: int | None = None,
        order_by: str = "updated_time",
        order_dir: str = "ASC",
    ) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(
            "/notes",
            {"fields": ",".join(fields), "order_by": order_by, "order_dir": order_dir},
            limit,
        )

    def get_folders(
        self, *, fields: Sequence[str], limit: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        return self._paginate("/folders", {"fields": ",".join(fields)}, limit)

    def get_tags(
        self, *, fields: Sequence[str], limit: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        return self._paginate("/tags", {"fields": ",".join(fields)}, limit)

    def get_tag_notes(
        self, tag_id: str, *, fields: Sequence[str], limit: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(f"/tags/{tag_id}/notes", {"fields": ",".join(fields)}, limit)

    async def get_events(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        """GET /events; returns ``{"items": [...], "cursor": str|None, "has_more": bool}``.

        A 400 from this endpoint is mapped to :class:`JoplinCursorInvalidError` so the
        indexer can fall back to a full rebuild (PRD §17.4). The caller owns cursor
        persistence.
        """
        params: dict[str, QueryValue] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await self._request(
            "/events",
            params=params,
            timeout=httpx.Timeout(self._event_poll_timeout, connect=5.0),
            cursor_path=True,
        )
        data: dict[str, Any] = resp.json()
        return {
            "items": data.get("items", []),
            "cursor": data.get("cursor"),
            "has_more": bool(data.get("has_more")),
        }
