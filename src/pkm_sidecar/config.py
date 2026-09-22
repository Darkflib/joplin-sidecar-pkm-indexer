"""Configuration loading with precedence: CLI > env > TOML > defaults (PRD §7).

The precedence is hand-rolled (not ``pydantic-settings``) so the layering is
explicit and testable. The resulting :class:`AppConfig` is immutable and stores
both tokens as :class:`~pydantic.SecretStr` so they cannot leak through ``repr``
or ``model_dump`` (PRD §8.3).

Bind validation: this module owns the pure :func:`is_loopback_host` predicate
(no FastAPI/httpx imports). The security subsystem imports it so there is a
single definition of "what counts as localhost".

Ephemeral sidecar API token: per the architecture decision, config does *not*
mint a token. When ``PKM_SIDECAR_API_TOKEN`` is unset, ``server.api_token`` is
``None``; the security subsystem mints an ephemeral token at startup (PRD §8.2).
"""

from __future__ import annotations

import ipaddress
import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl as _AnyHttpUrl
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter, field_validator

from pkm_sidecar.errors import ConfigError, scrub_url

logger = logging.getLogger("pkm_sidecar.config")

DEFAULT_DB_PATH = Path("~/.local/share/pkm-sidecar/index.sqlite3")
DEFAULT_CONFIG_PATH = Path("~/.config/pkm-sidecar/config.toml")

_HTTP_URL_ADAPTER: TypeAdapter[_AnyHttpUrl] = TypeAdapter(_AnyHttpUrl)


# --- helpers ---------------------------------------------------------------


def is_loopback_host(host: str) -> bool:
    """Return True if *host* is a loopback bind address.

    Accepts the literal string ``localhost`` by convention (resolving it via DNS
    is unsafe — ``/etc/hosts`` could map it elsewhere) plus any address that
    :mod:`ipaddress` reports as loopback (``127.0.0.0/8``, ``::1``).
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def redact(value: str | SecretStr | None) -> str:
    """Format a token-like value as ``set`` / ``unset`` for status output."""
    if value is None:
        return "unset"
    if isinstance(value, SecretStr):
        return "set" if value.get_secret_value() else "unset"
    return "set" if value else "unset"


def _normalise_base_url(value: str) -> str:
    """Validate *value* is an http(s) URL and return it without a trailing slash."""
    _HTTP_URL_ADAPTER.validate_python(value)
    return value.rstrip("/")


# --- section models --------------------------------------------------------


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "127.0.0.1"
    port: int = 8765
    api_token: SecretStr | None = None
    allow_non_localhost: bool = False


class JoplinConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str = "http://127.0.0.1:41184"
    token: SecretStr | None = None
    event_poll_seconds: int = 10
    page_limit: int = 100

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        return _normalise_base_url(v)


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path = DEFAULT_DB_PATH

    @field_validator("path")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class LoggingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class IndexingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    stale_days: int = 90
    inbox_folder_names: list[str] = ["Inbox", "00 Inbox", "_Inbox"]
    # On `serve` startup, if the index has never been fully built and a Joplin
    # token is configured, kick off a one-time full rebuild so the launcher-managed
    # (serve-only) path backfills existing notes instead of staying empty.
    rebuild_on_empty_start: bool = True


class EnrichmentConfig(BaseModel):
    """Suggested titles and tags (docs/enrichment.md). Off by default.

    Enabling this sends note bodies to the configured Ollama host, which is a
    real change in where your content goes — hence opt-in, and hence
    :func:`warn_if_enrichment_endpoint_is_cleartext`.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    ollama_base_url: str = "http://127.0.0.1:11434"
    title_model: str = "llama3.1:8b"
    tag_model: str = "llama3.1:8b"
    embedding_model: str = "bge-large:335m-en-v1.5-fp16"
    max_tags_per_note: int = 3
    neighbours: int = 25
    # Defaults to suggestions.sqlite3 beside the index; see suggestions_db_path.
    db_path: Path | None = None

    @field_validator("ollama_base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        return _normalise_base_url(v)

    @field_validator("db_path")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return None if v is None else v.expanduser()


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    server: ServerConfig
    joplin: JoplinConfig
    database: DatabaseConfig
    logging: LoggingConfig
    indexing: IndexingConfig
    enrichment: EnrichmentConfig = EnrichmentConfig()
    # Source TOML file, if one was loaded (None means env/defaults only).
    config_path: Path | None = None
    # Always False from config: the security subsystem mints and tracks any
    # ephemeral token at runtime (PRD §8.2). Kept for status-payload symmetry.
    api_token_was_generated: bool = False


# --- raw-value resolution --------------------------------------------------

# Map env var -> (section, field). PKM_SIDECAR_CONFIG_PATH is handled
# separately because it selects the TOML file rather than a config field.
_ENV_MAP: dict[str, tuple[str, str]] = {
    "PKM_SIDECAR_HOST": ("server", "host"),
    "PKM_SIDECAR_PORT": ("server", "port"),
    "PKM_SIDECAR_API_TOKEN": ("server", "api_token"),
    "PKM_SIDECAR_DB_PATH": ("database", "path"),
    "PKM_SIDECAR_LOG_LEVEL": ("logging", "level"),
    "JOPLIN_BASE_URL": ("joplin", "base_url"),
    "JOPLIN_TOKEN": ("joplin", "token"),
    "JOPLIN_EVENT_POLL_SECONDS": ("joplin", "event_poll_seconds"),
    "JOPLIN_PAGE_LIMIT": ("joplin", "page_limit"),
    "PKM_SIDECAR_ENRICHMENT_ENABLED": ("enrichment", "enabled"),
    "PKM_SIDECAR_OLLAMA_BASE_URL": ("enrichment", "ollama_base_url"),
}

# Map CLI override key -> (section, field). Only flags the user actually set
# are passed; None values are ignored. (PRD §15.1 lists host/port/config/db/
# log-level; allow_non_localhost comes from the serve flag in PRD §7.3.)
_CLI_MAP: dict[str, tuple[str, str]] = {
    "host": ("server", "host"),
    "port": ("server", "port"),
    "db": ("database", "path"),
    "log_level": ("logging", "level"),
    "allow_non_localhost": ("server", "allow_non_localhost"),
}

# Fields where an empty string means "unset" (fall back to lower precedence /
# default) rather than an error. PKM_SIDECAR_PORT="" is deliberately NOT here:
# an empty port must surface as a validation error (PRD §7 decision).
_EMPTY_AS_UNSET: set[tuple[str, str]] = {
    ("enrichment", "ollama_base_url"),
    ("server", "api_token"),
    ("joplin", "token"),
    ("joplin", "base_url"),
    ("server", "host"),
    ("database", "path"),
    ("logging", "level"),
}


def _layer_env(layers: dict[str, dict[str, Any]], env: Mapping[str, str]) -> None:
    for var, (section, field) in _ENV_MAP.items():
        if var not in env:
            continue
        value = env[var]
        if value == "" and (section, field) in _EMPTY_AS_UNSET:
            continue
        layers[section][field] = value


def _layer_cli(layers: dict[str, dict[str, Any]], cli_overrides: Mapping[str, Any]) -> None:
    for key, (section, field) in _CLI_MAP.items():
        if key not in cli_overrides:
            continue
        value = cli_overrides[key]
        if value is None:
            continue
        layers[section][field] = value


def _load_toml(config_path: Path, *, explicit: bool) -> dict[str, Any]:
    """Read a TOML config file into nested section dicts.

    *explicit* is True when the user named the file (via --config or
    PKM_SIDECAR_CONFIG_PATH): then a missing/unreadable/malformed file is a hard
    error. When the default path is merely probed, a missing file is ignored.
    """
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"Config file not found: {config_path}") from None
        return {}
    except OSError as exc:
        raise ConfigError(f"Config file is unreadable: {config_path} ({exc})") from exc

    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Config file is not valid TOML: {config_path} ({exc})") from exc

    sections: dict[str, Any] = {}
    for section in ("server", "joplin", "database", "logging", "indexing", "enrichment"):
        if section in parsed and isinstance(parsed[section], dict):
            sections[section] = dict(parsed[section])
    return sections


def _ensure_directories(cfg: AppConfig) -> None:
    cfg.database.path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.config_path is not None:
        cfg.config_path.parent.mkdir(parents=True, exist_ok=True)


# --- entry point -----------------------------------------------------------


def load_config(
    *,
    config_path: Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Build the immutable :class:`AppConfig` from all configuration sources.

    Precedence, highest to lowest: ``cli_overrides`` > ``env`` > TOML > defaults.
    ``env`` defaults to :data:`os.environ`. Keys in ``cli_overrides`` whose value
    is ``None`` are treated as unset.
    """
    import os

    env = os.environ if env is None else env
    cli_overrides = {} if cli_overrides is None else cli_overrides

    # Resolve which TOML file to read and whether the choice was explicit.
    explicit_path = cli_overrides.get("config") or env.get("PKM_SIDECAR_CONFIG_PATH") or None
    chosen_path = (
        Path(explicit_path).expanduser() if explicit_path else DEFAULT_CONFIG_PATH.expanduser()
    )
    toml_sections = _load_toml(chosen_path, explicit=bool(explicit_path))

    # Start from TOML, overlay env, overlay CLI.
    layers: dict[str, dict[str, Any]] = {
        "server": dict(toml_sections.get("server", {})),
        "joplin": dict(toml_sections.get("joplin", {})),
        "database": dict(toml_sections.get("database", {})),
        "logging": dict(toml_sections.get("logging", {})),
        "indexing": dict(toml_sections.get("indexing", {})),
        "enrichment": dict(toml_sections.get("enrichment", {})),
    }
    _layer_env(layers, env)
    _layer_cli(layers, cli_overrides)

    try:
        cfg = AppConfig(
            server=ServerConfig(**layers["server"]),
            joplin=JoplinConfig(**layers["joplin"]),
            database=DatabaseConfig(**layers["database"]),
            logging=LoggingConfig(**layers["logging"]),
            indexing=IndexingConfig(**layers["indexing"]),
            enrichment=EnrichmentConfig(**layers["enrichment"]),
            config_path=chosen_path if toml_sections else None,
        )
    except ValueError as exc:
        # Pydantic ValidationError is a ValueError subclass.
        raise ConfigError(f"Invalid configuration: {exc}") from exc

    # Bind-address safety is enforced by `serve` (security.validate_bind_address →
    # exit 3), not here: non-serve commands (status, db path) don't bind, so a
    # non-local host in config should not stop them from loading.
    _ensure_directories(cfg)

    if cfg.joplin.token is None:
        logger.warning("No Joplin token configured; indexing operations will be unavailable.")

    return cfg


def suggestions_db_path(cfg: AppConfig) -> Path:
    """Where the enrichment store lives — beside the index unless overridden."""
    if cfg.enrichment.db_path is not None:
        return cfg.enrichment.db_path
    return cfg.database.path.parent / "suggestions.sqlite3"


def warn_if_enrichment_endpoint_is_cleartext(cfg: AppConfig, log: logging.Logger) -> bool:
    """Warn when note bodies would cross a network in cleartext (CWE-319).

    Ollama's API is unauthenticated, so plain HTTP to anything but loopback puts
    the full text of every indexed note on the wire for anyone on the path.
    Returns True if a warning was issued, so callers (doctor) can report it.
    """
    if not cfg.enrichment.enabled:
        return False
    url = cfg.enrichment.ollama_base_url
    parsed = _HTTP_URL_ADAPTER.validate_python(url)
    host = parsed.host or ""
    if parsed.scheme == "https" or is_loopback_host(host):
        return False
    # Scrubbed *here*, not at the call site: this helper is the one that renders
    # the endpoint, so leaving it to callers means every future caller has to
    # remember. AnyHttpUrl accepts userinfo, so the configured value can carry a
    # password, and the redaction filter only masks secrets registered with it.
    log.warning(
        "Enrichment will send note bodies to %s over plain HTTP to an unauthenticated "
        "API. Prefer loopback, an encrypted overlay (Tailscale/WireGuard), or an SSH "
        "tunnel rather than trusting the local network.",
        scrub_url(url),
    )
    return True


def require_joplin_token(cfg: AppConfig) -> str:
    """Return the Joplin token, or raise :class:`ConfigError` if it is missing.

    Read/serve callers tolerate a missing token; indexing, sync, and doctor
    callers invoke this so they fail clearly (PRD §7.3).
    """
    if cfg.joplin.token is None:
        raise ConfigError("Joplin token is required for indexing operations.")
    return cfg.joplin.token.get_secret_value()
