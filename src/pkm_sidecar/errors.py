"""Typed exception hierarchy — the single source of truth for the project.

Every subsystem imports its exceptions from here so that:

* the API layer can ``except PkmSidecarError`` for clean status-code mapping;
* there is exactly one class per failure mode (no parallel hierarchies with
  near-identical names — a conflict the planning critique flagged);
* :func:`to_http_status` centralises the error → HTTP status policy without
  importing FastAPI, so the CLI and ``doctor`` can import this module freely.

Hierarchy::

    PkmSidecarError
    ├── ConfigError
    ├── SecurityError
    ├── AuthError              (bearer-token check failed → 401)
    ├── NotFoundError          (resource not in the index → 404)
    ├── ConflictError          (→ 409)
    ├── IndexInProgressError   (rebuild already running → 409)
    ├── DatabaseError
    │   ├── DatabaseLockedError
    │   ├── SchemaVersionMismatchError
    │   └── FTSUnavailableError
    └── JoplinError            (.url is token-scrubbed, .status_code optional)
        ├── JoplinUnreachableError
        ├── JoplinAuthError
        ├── JoplinNotFoundError
        ├── JoplinRateLimitedError
        ├── JoplinCursorInvalidError
        └── JoplinBadResponseError
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = [
    "AuthError",
    "ConfigError",
    "ConflictError",
    "DatabaseError",
    "DatabaseLockedError",
    "FTSUnavailableError",
    "IndexInProgressError",
    "JoplinAuthError",
    "JoplinBadResponseError",
    "JoplinCursorInvalidError",
    "JoplinError",
    "JoplinNotFoundError",
    "JoplinRateLimitedError",
    "JoplinUnreachableError",
    "NotFoundError",
    "PkmSidecarError",
    "SchemaVersionMismatchError",
    "SecurityError",
    "scrub_token",
    "to_http_status",
]


# --- token scrubbing -------------------------------------------------------

# Matches `token=<value>` in a query string, keeping the key but masking the
# value. Defence in depth: even if an exception repr is logged by a library
# outside our redaction filter, the Joplin token never appears (PRD §8.3).
_TOKEN_QUERY_RE = re.compile(r"(?i)(token=)[^&\s]+")


def scrub_token(text: str) -> str:
    """Replace any ``token=<value>`` occurrence in *text* with ``token=***``."""
    return _TOKEN_QUERY_RE.sub(r"\1***", text)


# --- base ------------------------------------------------------------------


class PkmSidecarError(Exception):
    """Base class for all pkm-sidecar errors."""


# --- configuration / security ---------------------------------------------


class ConfigError(PkmSidecarError):
    """Invalid or missing configuration detected at load time (PRD §7.3)."""


class SecurityError(PkmSidecarError):
    """A security invariant was violated (e.g. non-local bind, blocked transport)."""


class AuthError(PkmSidecarError):
    """A sidecar API bearer-token check failed (PRD §8.2) → HTTP 401.

    ``reason`` records *why* without ever storing the offered token value.
    """

    def __init__(
        self,
        message: str = "Authentication required.",
        *,
        reason: Literal["missing", "invalid", "disabled"] = "missing",
    ) -> None:
        super().__init__(message)
        self.reason = reason


# --- generic API-layer errors ---------------------------------------------


class NotFoundError(PkmSidecarError):
    """A requested resource is not present in the index → HTTP 404."""


class ConflictError(PkmSidecarError):
    """The request conflicts with current state → HTTP 409."""


class IndexInProgressError(ConflictError):
    """A rebuild/sync is already running (PRD §11) → HTTP 409."""


# --- database --------------------------------------------------------------


class DatabaseError(PkmSidecarError):
    """Base for SQLite/persistence errors."""


class DatabaseLockedError(DatabaseError):
    """SQLite remained locked after the retry budget was exhausted (PRD §17.3)."""


class SchemaVersionMismatchError(DatabaseError):
    """The on-disk schema version is incompatible with this code (forward guard)."""


class FTSUnavailableError(DatabaseError):
    """SQLite was built without FTS5 support (PRD §15.5 doctor check)."""


# --- Joplin Data API -------------------------------------------------------


class JoplinError(PkmSidecarError):
    """Base for Joplin Data API problems.

    The stored ``url`` is always token-scrubbed so that a bare stack trace can
    never leak the Joplin token (PRD §8.3).
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.url = scrub_token(url) if url is not None else None
        self.status_code = status_code


class JoplinUnreachableError(JoplinError):
    """Connection/timeout/DNS failure reaching Joplin (PRD §17.1) → HTTP 503."""


class JoplinAuthError(JoplinError):
    """Joplin returned 401/403 — the Joplin token is invalid (PRD §17.2) → HTTP 502."""


class JoplinNotFoundError(JoplinError):
    """Joplin returned 404 for the requested entity."""


class JoplinRateLimitedError(JoplinError):
    """Joplin returned 429/503 — retry later, not immediately."""


class JoplinCursorInvalidError(JoplinError):
    """The event cursor is too old / invalid (PRD §17.4); indexer triggers a rebuild."""


class JoplinBadResponseError(JoplinError):
    """Joplin returned a response that could not be parsed as expected."""


# --- HTTP status policy ----------------------------------------------------

# Most specific first. :func:`to_http_status` walks the exception's MRO and
# returns the first match, so subclasses inherit their parent's status unless
# they appear here explicitly.
_STATUS_TABLE: tuple[tuple[type[PkmSidecarError], int], ...] = (
    (AuthError, 401),
    (NotFoundError, 404),
    (JoplinNotFoundError, 404),
    (IndexInProgressError, 409),
    (ConflictError, 409),
    (JoplinAuthError, 502),
    (JoplinUnreachableError, 503),
    (JoplinRateLimitedError, 503),
    (DatabaseLockedError, 503),
)


def to_http_status(exc: PkmSidecarError) -> int:
    """Map a :class:`PkmSidecarError` to an HTTP status code.

    Pure and FastAPI-free so it is importable from the CLI. The api subsystem
    registers the exception handlers that consume this.
    """
    for klass in type(exc).__mro__:
        for mapped, status in _STATUS_TABLE:
            if klass is mapped:
                return status
    return 500
