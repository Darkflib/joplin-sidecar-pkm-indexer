"""Security primitives (PRD §8): bind validation, ephemeral token, bearer auth,
and the outbound-request guard.

Single source for the project's security invariants:

* **Localhost-only** — :func:`validate_bind_address` and the config loader both
  reject non-loopback binds unless ``--allow-non-localhost`` is set, and a
  non-local bind additionally requires a *configured* (non-ephemeral) API token.
* **Bearer auth** — :func:`verify_bearer_token` (constant-time) raises
  :class:`~pkm_sidecar.errors.AuthError`, which the API layer maps to 401.
* **No external network (PRD §8.5)** — :class:`SingleOriginTransport` and
  :func:`assert_url_matches_base` refuse any request whose URL is not the one
  origin that transport was built for. The Joplin client gets one fenced to the
  Joplin base; enrichment gets a *separate* instance fenced to Ollama. The fence
  is per-client, so adding a second origin never widens the first.
* **Token hygiene** — the ephemeral API token is minted here, written to a 0600
  file, registered with the canonical redaction filter, and never logged.

This module does not own a redaction filter; :func:`register_secret` delegates
to :func:`pkm_sidecar.logging_config.add_secret` (the one canonical filter).
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import secrets
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from pkm_sidecar import logging_config
from pkm_sidecar.config import AppConfig, is_loopback_host
from pkm_sidecar.errors import AuthError, SecurityError

# Default ports used to normalise URL comparison.
_DEFAULT_PORTS = {"http": 80, "https": 443}


# --- secret registration ---------------------------------------------------


def register_secret(value: str) -> None:
    """Register *value* with the canonical redaction filter (PRD §8.3)."""
    logging_config.add_secret(value)


# --- bind-address validation -----------------------------------------------


def validate_bind_address(host: str, allow_non_localhost: bool) -> str:
    """Return *host* if it is a safe bind address, else raise :class:`SecurityError`.

    Loopback hosts always pass. Anything else requires the explicit
    ``allow_non_localhost`` override (PRD §8.1).
    """
    if is_loopback_host(host) or allow_non_localhost:
        return host
    raise SecurityError(
        f"Refusing to bind to non-localhost host {host!r}. Pass --allow-non-localhost to override."
    )


def assert_non_local_bind_has_token(config: AppConfig) -> None:
    """Refuse to expose a non-localhost bind protected only by an ephemeral token.

    Binding outside loopback means the API is reachable by other hosts; an
    auto-generated token the operator never saw is not adequate protection
    (PRD §8.1/§8.2).

    Phrased against the *configuration* rather than a :class:`ResolvedToken` so
    it can run before :func:`resolve_api_token` — the two are equivalent, since
    that function mints an ephemeral token exactly when no token is configured,
    and checking first means a refused bind never writes a token file it is
    about to abandon.
    """
    if not is_loopback_host(config.server.host) and config.server.api_token is None:
        raise SecurityError(
            "Refusing to expose a non-localhost bind with an ephemeral API token. "
            "Set PKM_SIDECAR_API_TOKEN explicitly."
        )


# --- ephemeral API token ----------------------------------------------------


@dataclass(frozen=True)
class ResolvedToken:
    """The API token in effect, plus where it came from."""

    token: str
    source: Literal["config", "ephemeral"]
    created_at: int


def generate_ephemeral_token(nbytes: int = 32) -> str:
    """Return a URL-safe random token (PRD §8.2)."""
    return secrets.token_urlsafe(nbytes)


def _runtime_dir() -> Path:
    """Return the per-user runtime directory for transient files (token file).

    ``PKM_SIDECAR_RUNTIME_DIR`` overrides everything (used by tests and the
    launcher). Otherwise: ``XDG_RUNTIME_DIR`` or ``~/.cache`` on Linux,
    ``~/Library/Application Support`` on macOS, ``%LOCALAPPDATA%`` on Windows.
    """
    override = os.environ.get("PKM_SIDECAR_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/pkm-sidecar").expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (
            Path(base) / "pkm-sidecar" if base else Path("~/AppData/Local/pkm-sidecar").expanduser()
        )
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    return Path(xdg) / "pkm-sidecar" if xdg else Path("~/.cache/pkm-sidecar").expanduser()


def ephemeral_token_file_path(config: AppConfig) -> Path:
    """Deterministic path for the ephemeral token file, keyed by port."""
    return _runtime_dir() / f"pkm-sidecar-{config.server.port}.token"


def write_token_file(path: Path, token: str) -> None:
    """Write *token* atomically with 0600 permissions (best-effort on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".token-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def remove_token_file(path: Path) -> None:
    """Remove the ephemeral token file on shutdown; ignore if already gone."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def resolve_api_token(config: AppConfig, logger: logging.Logger) -> ResolvedToken:
    """Resolve the sidecar API token, minting an ephemeral one if none configured.

    Never logs the token value — only its source and (for ephemeral) the file
    path. Registers the value with the redaction filter before returning.
    """
    configured = config.server.api_token
    if configured is not None:
        token = configured.get_secret_value()
        register_secret(token)
        logger.info("Using configured sidecar API token.")
        return ResolvedToken(token=token, source="config", created_at=int(time.time()))

    token = generate_ephemeral_token()
    register_secret(token)
    path = ephemeral_token_file_path(config)
    write_token_file(path, token)
    logger.warning(
        "No sidecar API token configured; generated an ephemeral token at %s "
        "(set PKM_SIDECAR_API_TOKEN to override).",
        path,
    )
    return ResolvedToken(token=token, source="ephemeral", created_at=int(time.time()))


# --- bearer authentication --------------------------------------------------


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison (defeats timing attacks)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_bearer_token(authorization_header: str | None, expected_token: str | None) -> None:
    """Validate an ``Authorization: Bearer <token>`` header (PRD §8.2).

    Raises :class:`~pkm_sidecar.errors.AuthError` (mapped to 401 by the API)
    without ever echoing the offered token.
    """
    if not expected_token:
        raise AuthError("API token is not configured.", reason="disabled")
    if not authorization_header:
        raise AuthError("Missing bearer token.", reason="missing")
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Missing bearer token.", reason="missing")
    if not constant_time_compare(token, expected_token):
        raise AuthError("Invalid bearer token.", reason="invalid")


# --- outbound-request guard (PRD §8.5) --------------------------------------


def _url_parts(url: str | httpx.URL) -> tuple[str, str, int]:
    parsed = httpx.URL(url) if isinstance(url, str) else url
    scheme = parsed.scheme
    host = parsed.host
    port = parsed.port if parsed.port is not None else _DEFAULT_PORTS.get(scheme, 0)
    return scheme, host, port


def assert_url_matches_base(url: str | httpx.URL, base_url: str) -> None:
    """Raise :class:`SecurityError` unless *url* targets exactly *base_url*'s origin."""
    if _url_parts(url) != _url_parts(base_url):
        target = httpx.URL(url) if isinstance(url, str) else url
        allowed = httpx.URL(base_url)
        raise SecurityError(
            f"Blocked outbound request to {target.scheme}://{target.host}:{target.port or ''}; "
            f"this client may only reach {allowed.scheme}://{allowed.host}:{allowed.port or ''}."
        )


class SingleOriginTransport(httpx.AsyncBaseTransport):
    """httpx transport that refuses any request outside the origin it was built for.

    Runtime enforcement of "no external network access" (PRD §8.5). Each client
    owns its own instance, so the Joplin client can only reach Joplin and the
    enrichment client can only reach Ollama — a second permitted origin is a
    second transport, never a wider allowlist on the first.
    """

    def __init__(self, base_url: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert_url_matches_base(request.url, self._base_url)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- config file permissions ------------------------------------------------


def warn_if_world_readable_config(path: Path, logger: logging.Logger) -> None:
    """Warn (POSIX only) if the config file is group/other-accessible (PRD §8.3).

    Never reads the file content — only its mode bits.
    """
    if os.name != "posix":
        logger.info("Config file permissions not checked on this platform: %s", path)
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "Config file %s is accessible by group/other; it may contain a token. "
            "Run: chmod 600 %s",
            path,
            path,
        )
