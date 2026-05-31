"""API error envelope and exception handlers (PRD §13, §17).

All non-2xx ``/api`` responses share ``{"error": {"code", "message"}}`` with
stable code strings. The catch-all handler logs the exception but never leaks its
args into the response body.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pkm_sidecar.errors import (
    AuthError,
    ConflictError,
    DatabaseLockedError,
    JoplinNotFoundError,
    JoplinUnreachableError,
    NotFoundError,
    PkmSidecarError,
    to_http_status,
)
from pkm_sidecar.logging_config import get_logger

logger = get_logger("api")


def error_code(exc: PkmSidecarError) -> str:
    """Stable error-code string for the envelope."""
    if isinstance(exc, AuthError):
        return "unauthorized"
    if isinstance(exc, NotFoundError | JoplinNotFoundError):
        return "not_found"
    if isinstance(exc, ConflictError):  # includes IndexInProgressError
        return "conflict"
    if isinstance(exc, JoplinUnreachableError):
        return "joplin_unreachable"
    if isinstance(exc, DatabaseLockedError):
        return "database_locked"
    return "internal_error"


def _envelope(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PkmSidecarError)
    async def _handle_known(_request: Request, exc: PkmSidecarError) -> JSONResponse:
        return _envelope(error_code(exc), str(exc), to_http_status(exc))

    @app.exception_handler(Exception)
    async def _handle_unknown(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API error")
        return _envelope("internal_error", "Internal server error.", 500)
