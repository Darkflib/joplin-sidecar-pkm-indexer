"""Logging setup, token redaction, and shared resilience helpers (PRD §16, §17).

This module is the single owner of:

* :class:`TokenRedactionFilter` — the one canonical secret-redaction mechanism.
  The security subsystem registers the ephemeral API token here via
  :func:`add_secret`; it must not maintain a parallel filter.
* :func:`configure_logging` — idempotent setup that installs a stderr handler
  with the PRD §16 format *and* a :class:`logging.LogRecordFactory` so that
  every record (ours, uvicorn's, httpx's, and pytest's ``caplog``) is scrubbed
  at creation, before any handler sees it.
* :func:`log_event` / :data:`EventName` — the stable vocabulary of log events.
* :func:`retry_with_backoff` and :class:`LogOnceGuard` — used by the Joplin
  client (transient retries) and the db layer (locked-database retries), and to
  suppress log spam from outage loops (PRD §17.1/§17.2).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shlex
import threading
import time
import traceback
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal

# Required event names (PRD §16). Every subsystem must use one of these so log
# scraping stays stable.
EventName = Literal[
    "service.start",
    "service.stop",
    "config.loaded",
    "db.opened",
    "db.locked_retry",
    "index.full.started",
    "index.full.completed",
    "index.full.failed",
    "index.incremental.started",
    "index.incremental.completed",
    "index.incremental.failed",
    "enrichment.titles.completed",
    "enrichment.embeddings.completed",
    "joplin.unreachable",
    "joplin.token_invalid",
    "api.auth_failed",
    "event.cursor_invalid",
    "exception.unexpected",
]

# Fields that must never be logged (PRD §16, §21).
_FORBIDDEN_FIELDS = frozenset(
    {"body", "content", "note_body", "token", "authorization", "password"}
)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_AUTH_BEARER_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")
_MAX_LINE_CHARS = 2048
_REDACTED = "***REDACTED***"


# --- redaction -------------------------------------------------------------


class TokenRedactionFilter(logging.Filter):
    """Replaces registered secret strings anywhere in a log record.

    Scrubs ``record.msg``, ``record.args``, formatted ``exc_info`` text, and any
    string attribute in ``record.__dict__`` (covering ``extra=`` fields). Also
    masks ``Authorization: Bearer …`` headers unconditionally. Thread-safe.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add_secret(self, value: str) -> None:
        """Register a non-empty secret to redact (length-agnostic per PRD §8.3)."""
        if value:
            with self._lock:
                self._secrets.add(value)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def _redact_text(self, text: str) -> str:
        with self._lock:
            secrets = tuple(self._secrets)
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return _AUTH_BEARER_RE.sub(rf"\1{_REDACTED}", text)

    def scrub_record(self, record: logging.LogRecord) -> None:
        for key, value in list(record.__dict__.items()):
            if isinstance(value, str):
                redacted = self._redact_text(value)
                if redacted != value:
                    record.__dict__[key] = redacted
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (self._redact_text(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact_text(a) if isinstance(a, str) else a for a in record.args
                )
        if record.exc_info and record.exc_info[0] is not None:
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redact_text(text)

    def filter(self, record: logging.LogRecord) -> bool:
        self.scrub_record(record)
        return True


class MaxLineLengthFilter(logging.Filter):
    """Truncates over-long records — defence-in-depth against a logged note body."""

    def __init__(self, max_chars: int = _MAX_LINE_CHARS) -> None:
        super().__init__()
        self.max_chars = max_chars

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed args
            return True
        if len(message) > self.max_chars:
            dropped = len(message) - self.max_chars
            record.msg = message[: self.max_chars] + f"... [truncated {dropped} chars]"
            record.args = None
        return True


class _UtcFormatter(logging.Formatter):
    """PRD §16 format with an ISO-8601 UTC timestamp (``…sssZ``)."""

    def __init__(self) -> None:
        super().__init__(_LOG_FORMAT)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = time.gmtime(record.created)
        base = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}Z"


# Module singletons.
_REDACTION_FILTER = TokenRedactionFilter()
_LENGTH_FILTER = MaxLineLengthFilter()
_handler: logging.Handler | None = None
_orig_factory: Callable[..., logging.LogRecord] | None = None
_configured = False


def add_secret(value: str) -> None:
    """Register a secret with the canonical redaction filter (security calls this)."""
    _REDACTION_FILTER.add_secret(value)


def _install_record_factory() -> None:
    global _orig_factory
    if _orig_factory is not None:
        return
    _orig_factory = logging.getLogRecordFactory()
    captured = _orig_factory

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = captured(*args, **kwargs)
        _REDACTION_FILTER.scrub_record(record)
        return record

    logging.setLogRecordFactory(factory)


def configure_logging(level: str = "INFO", *, redact_values: Iterable[str] = ()) -> None:
    """Configure logging idempotently. Call before constructing HTTP/SQL clients.

    Installs the stderr handler, the redaction record factory, and the redaction
    filter on the root logger so secrets are scrubbed everywhere.
    """
    global _handler, _configured

    for value in redact_values:
        _REDACTION_FILTER.add_secret(value)

    root = logging.getLogger()
    if not _configured:
        _install_record_factory()
        handler = logging.StreamHandler()
        handler.setFormatter(_UtcFormatter())
        handler.addFilter(_REDACTION_FILTER)
        handler.addFilter(_LENGTH_FILTER)
        root.addHandler(handler)
        root.addFilter(_REDACTION_FILTER)
        _handler = handler
        _configured = True

    numeric = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric)
    if _handler is not None:
        _handler.setLevel(numeric)
    logging.getLogger("pkm_sidecar").setLevel(numeric)
    # Quiet chatty third-party loggers (PRD: httpx INFO→WARNING).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def reset_for_tests() -> None:
    """Tear down logging configuration so tests start from a clean slate."""
    global _handler, _orig_factory, _configured
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler = None
    if _REDACTION_FILTER in root.filters:
        root.removeFilter(_REDACTION_FILTER)
    _REDACTION_FILTER.clear()
    if _orig_factory is not None:
        logging.setLogRecordFactory(_orig_factory)
        _orig_factory = None
    _configured = False


def get_logger(name: str) -> logging.Logger:
    """Return the canonical ``pkm_sidecar.<name>`` logger."""
    return logging.getLogger(f"pkm_sidecar.{name}")


# --- structured events -----------------------------------------------------

_forbidden_field_guard_lock = threading.Lock()
_warned_forbidden_fields: set[str] = set()


def _warn_forbidden_once(field: str, logger: logging.Logger) -> None:
    with _forbidden_field_guard_lock:
        already = field in _warned_forbidden_fields
        _warned_forbidden_fields.add(field)
    if not already:
        logger.warning("Refusing to log forbidden field %r (logged once).", field)


def log_event(
    logger: logging.Logger,
    event: EventName,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a canonical §16 event as ``event k=v …`` with shell-safe quoting.

    Fields named body/content/note_body/token/authorization/password are dropped
    (with a one-time warning) so a note body or secret can't slip into a log.
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _FORBIDDEN_FIELDS:
            _warn_forbidden_once(key, logger)
            continue
        safe[key] = value
    suffix = " " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in safe.items()) if safe else ""
    logger.log(level, "%s%s", event, suffix)


# --- resilience helpers ----------------------------------------------------


async def retry_with_backoff[T](
    coro_fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 0.25,
    max_delay: float = 5.0,
    jitter: float = 0.1,
    retry_on: tuple[type[BaseException], ...],
    logger: logging.Logger,
    op: str,
) -> T:
    """Run ``coro_fn`` with exponential backoff on ``retry_on`` exceptions.

    ``asyncio.CancelledError`` is always re-raised immediately and must never be
    included in ``retry_on``. The last exception is re-raised unwrapped once the
    retry budget is exhausted (PRD §10 "retry transient failures conservatively").
    """
    attempt = 0
    while True:
        try:
            return await coro_fn()
        except asyncio.CancelledError:
            raise
        except retry_on as exc:
            attempt += 1
            if attempt > retries:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + random.uniform(0, jitter)
            logger.warning(
                "retry op=%s attempt=%d/%d error=%s",
                op,
                attempt,
                retries,
                type(exc).__name__,
            )
            await asyncio.sleep(delay)


class LogOnceGuard:
    """Rate-limits repeated log lines per key (PRD §17.2 'do not spam logs')."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_log(self, key: str, *, cooldown_seconds: float = 300.0) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is None or (now - last) >= cooldown_seconds:
                self._last[key] = now
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._last.clear()
