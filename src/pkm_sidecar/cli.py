"""Command-line interface (Typer) — PRD §15.

Commands: ``serve``, ``status``, ``doctor``, ``open``, ``index rebuild``,
``index sync``, ``db path``. Every command configures logging first, then loads
config (CLI > env > TOML > defaults), then runs security checks before work.

Exit codes: 0 ok · 1 runtime/operational failure · 2 config/usage error · 3 unsafe
non-localhost bind (missing --allow-non-localhost, or no configured API token) ·
4 indexing finished with errors · 5 event cursor invalid · 130 interrupted.
"""

from __future__ import annotations

import asyncio
import os
import webbrowser
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal

import typer
import uvicorn

from pkm_sidecar import __version__, db, security
from pkm_sidecar.config import (
    AppConfig,
    is_loopback_host,
    load_config,
    require_joplin_token,
    warn_if_enrichment_endpoint_is_cleartext,
)
from pkm_sidecar.errors import (
    ConfigError,
    JoplinAuthError,
    JoplinCursorInvalidError,
    JoplinError,
    JoplinUnreachableError,
    OllamaError,
    SecurityError,
    scrub_url,
)
from pkm_sidecar.joplin_client import JoplinClient
from pkm_sidecar.logging_config import configure_logging, get_logger
from pkm_sidecar.repositories import NoteRepository

logger = get_logger("cli")

_EPILOG = (
    "Exit codes: 0 ok · 1 runtime failure · 2 config error · 3 unsafe non-localhost "
    "bind · 4 indexing errors · 5 cursor invalid · 130 interrupted."
)

app = typer.Typer(
    name="pkm-sidecar",
    help="Local-first read-only Joplin PKM indexer and dashboard.",
    no_args_is_help=True,
    add_completion=False,
    epilog=_EPILOG,
)
index_app = typer.Typer(help="Index management.", no_args_is_help=True)
db_app = typer.Typer(help="Database utilities.", no_args_is_help=True)
app.add_typer(index_app, name="index")
app.add_typer(db_app, name="db")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pkm-sidecar {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version."
    ),
) -> None:
    """pkm-sidecar root command."""


# --- shared helpers --------------------------------------------------------


def _prepare(
    *,
    config: str | None = None,
    db_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    log_level: str | None = None,
    allow_non_localhost: bool = False,
) -> AppConfig:
    """Configure logging, load config, register the Joplin token for redaction."""
    configure_logging(log_level or "INFO")
    overrides: dict[str, Any] = {
        "config": config,
        "db": db_path,
        "host": host,
        "port": port,
        "log_level": log_level,
    }
    if allow_non_localhost:
        overrides["allow_non_localhost"] = True
    overrides = {k: v for k, v in overrides.items() if v is not None}
    try:
        cfg = load_config(cli_overrides=overrides)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    redact = [cfg.joplin.token.get_secret_value()] if cfg.joplin.token else []
    configure_logging(cfg.logging.level, redact_values=redact)
    return cfg


def _run_async[T](coro: Awaitable[T]) -> T:
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    except KeyboardInterrupt as exc:
        raise typer.Exit(130) from exc


def _make_client(cfg: AppConfig, token: str) -> JoplinClient:
    return JoplinClient(
        cfg.joplin.base_url,
        token,
        timeout=10.0,
        event_poll_timeout=cfg.joplin.event_poll_seconds,
        page_limit=cfg.joplin.page_limit,
    )


# --- serve -----------------------------------------------------------------


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    config: str | None = typer.Option(None, "--config"),
    db_path: str | None = typer.Option(None, "--db"),
    log_level: str | None = typer.Option(None, "--log-level"),
    allow_non_localhost: bool = typer.Option(False, "--allow-non-localhost"),
) -> None:
    """Start the local FastAPI server."""
    from pkm_sidecar.app import create_app

    cfg = _prepare(
        config=config,
        db_path=db_path,
        host=host,
        port=port,
        log_level=log_level,
        allow_non_localhost=allow_non_localhost,
    )
    try:
        security.validate_bind_address(cfg.server.host, cfg.server.allow_non_localhost)
        # An overridden bind is still refused when the only thing guarding it
        # would be an auto-generated token the operator never saw.
        security.assert_non_local_bind_has_token(cfg)
    except SecurityError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(3) from exc

    server = uvicorn.Server(
        uvicorn.Config(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_config=None)
    )
    try:
        _run_async(server.serve())
    except OSError as exc:
        typer.secho(
            f"Failed to bind {cfg.server.host}:{cfg.server.port}: {exc}", fg="red", err=True
        )
        raise typer.Exit(1) from exc


# --- index rebuild / sync --------------------------------------------------


async def _do_rebuild(cfg: AppConfig) -> int:
    from pkm_sidecar import services

    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    client = _make_client(cfg, require_joplin_token(cfg))
    try:
        result = await services.full_rebuild(conn, client, cfg)
    except JoplinUnreachableError as exc:
        typer.secho(f"Joplin unreachable: {exc}", fg="red", err=True)
        return 1
    except JoplinError as exc:
        typer.secho(f"Joplin error: {type(exc).__name__}", fg="red", err=True)
        return 1
    finally:
        await client.aclose()
        conn.close()
    typer.echo(
        f"Rebuild {result.status}: notes={result.notes_seen} folders={result.folders_seen} "
        f"tags={result.tags_seen} updated={result.notes_updated} errors={result.errors}"
    )
    return 0 if result.status == "success" else 4


async def _do_sync(cfg: AppConfig) -> int:
    from pkm_sidecar import services

    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    client = _make_client(cfg, require_joplin_token(cfg))
    try:
        result = await services.incremental_sync_once(conn, client, cfg)
    except JoplinCursorInvalidError:
        typer.secho(
            "Event cursor invalid — run `pkm-sidecar index rebuild`.", fg="yellow", err=True
        )
        return 5
    except JoplinUnreachableError as exc:
        typer.secho(f"Joplin unreachable: {exc}", fg="red", err=True)
        return 1
    finally:
        await client.aclose()
        conn.close()
    typer.echo(f"Sync {result.status}: notes={result.notes_seen} errors={result.errors}")
    return 0 if result.status == "success" else 4


@index_app.command("rebuild")
def index_rebuild(
    config: str | None = typer.Option(None, "--config"),
    db_path: str | None = typer.Option(None, "--db"),
    log_level: str | None = typer.Option(None, "--log-level"),
) -> None:
    """Run a full index rebuild, then exit."""
    cfg = _prepare(config=config, db_path=db_path, log_level=log_level)
    try:
        require_joplin_token(cfg)
    except ConfigError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    raise typer.Exit(_run_async(_do_rebuild(cfg)))


@index_app.command("sync")
def index_sync(
    config: str | None = typer.Option(None, "--config"),
    db_path: str | None = typer.Option(None, "--db"),
    log_level: str | None = typer.Option(None, "--log-level"),
) -> None:
    """Run one incremental sync pass, then exit."""
    cfg = _prepare(config=config, db_path=db_path, log_level=log_level)
    try:
        require_joplin_token(cfg)
    except ConfigError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    raise typer.Exit(_run_async(_do_sync(cfg)))


# --- status ----------------------------------------------------------------


async def _gather_status(cfg: AppConfig) -> dict[str, Any]:
    from pkm_sidecar import __version__ as version

    db.init_db(cfg.database.path)  # idempotent; a fresh machine shows zero counts
    reader = db.open_reader_connection(cfg.database.path)
    try:
        repo = NoteRepository(reader)
        counts = repo.get_status_counts()
        last_full = repo.get_meta(db.META_LAST_FULL_INDEX_AT)
        last_inc = repo.get_meta(db.META_LAST_INCREMENTAL_INDEX_AT)
        last_event = repo.get_meta(db.META_LAST_EVENT_ID)
    finally:
        reader.close()
    token = cfg.joplin.token.get_secret_value() if cfg.joplin.token else ""
    client = _make_client(cfg, token)
    try:
        reachable = await client.ping()
    finally:
        await client.aclose()
    return {
        "service": "pkm-sidecar",
        "version": version,
        "joplin": {
            "configured": bool(cfg.joplin.token),
            "reachable": reachable,
            "base_url": cfg.joplin.base_url,
            "last_error": None,
        },
        "database": {
            "path": str(cfg.database.path),
            "note_count": counts.note_count,
            "folder_count": counts.folder_count,
            "tag_count": counts.tag_count,
            "task_count": counts.task_count,
            "resource_count": counts.resource_count,
        },
        "indexing": {
            "last_full_index_at": int(last_full) if last_full else None,
            "last_incremental_index_at": int(last_inc) if last_inc else None,
            "last_event_id": last_event,
        },
    }


@app.command()
def status(
    config: str | None = typer.Option(None, "--config"),
    db_path: str | None = typer.Option(None, "--db"),
    json_output: bool = typer.Option(False, "--json", help="Emit the /api/status payload as JSON."),
) -> None:
    """Print database and Joplin status."""
    cfg = _prepare(config=config, db_path=db_path)
    payload = _run_async(_gather_status(cfg))
    if json_output:
        import json

        typer.echo(json.dumps(payload, indent=2))
        return
    j, d, ix = payload["joplin"], payload["database"], payload["indexing"]
    typer.echo(f"service: {payload['service']} v{payload['version']}")
    typer.echo(
        f"joplin:  configured={j['configured']} reachable={j['reachable']} url={j['base_url']}"
    )
    typer.echo(
        f"db:      notes={d['note_count']} folders={d['folder_count']} "
        f"tags={d['tag_count']} tasks={d['task_count']} resources={d['resource_count']}"
    )
    typer.echo(
        f"index:   last_full={ix['last_full_index_at']} "
        f"last_incremental={ix['last_incremental_index_at']} cursor={ix['last_event_id']}"
    )


# --- doctor ----------------------------------------------------------------


@dataclass
class DoctorResult:
    """One doctor check.

    ``WARN`` is advisory and does **not** affect the exit code: it marks a
    deliberate trade-off the operator may have chosen knowingly (plain HTTP to a
    model host on a private network, say) rather than something broken. Only
    ``FAIL`` means "this will not work, or is unsafe regardless of intent".
    """

    status: Literal["OK", "WARN", "FAIL", "SKIP"]
    name: str
    message: str


async def _run_doctor(cfg: AppConfig) -> list[DoctorResult]:
    results: list[DoctorResult] = []
    results.append(DoctorResult("OK", "config", "configuration loaded"))

    # DB directory writable.
    data_dir = cfg.database.path.parent
    if os.access(data_dir, os.W_OK):
        results.append(DoctorResult("OK", "db_dir_writable", str(data_dir)))
    else:
        results.append(DoctorResult("FAIL", "db_dir_writable", f"not writable: {data_dir}"))

    # FTS5 availability.
    db.init_db(cfg.database.path)
    conn = db.open_writer_connection(cfg.database.path)
    try:
        fts_ok = db.fts_available(conn)
        interrupted_since = db.rebuild_in_progress_since(conn)
    finally:
        conn.close()
    results.append(
        DoctorResult(
            "OK" if fts_ok else "FAIL", "fts5_available", "FTS5 build" if fts_ok else "no FTS5"
        )
    )

    # An index left half-wiped by an interrupted rebuild still answers every
    # query, just with partial tasks/links/tags, so say so plainly here.
    if interrupted_since is None:
        results.append(DoctorResult("OK", "index_complete", "derived tables intact"))
    else:
        results.append(
            DoctorResult(
                "FAIL",
                "index_complete",
                f"a full rebuild started at {interrupted_since} never finished; "
                "tasks/links/tags are partial — run `pkm-sidecar index rebuild`",
            )
        )

    # Bind address local.
    if is_loopback_host(cfg.server.host) or cfg.server.allow_non_localhost:
        results.append(DoctorResult("OK", "bind_local", cfg.server.host))
    else:
        results.append(
            DoctorResult("FAIL", "bind_local", f"{cfg.server.host} is not loopback (no override)")
        )

    # Config file permissions (POSIX only).
    if cfg.config_path is None:
        results.append(DoctorResult("SKIP", "config_perms", "no config file"))
    elif os.name != "posix":
        results.append(DoctorResult("SKIP", "config_perms", "not checked on this platform"))
    else:
        import stat

        mode = cfg.config_path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            results.append(
                DoctorResult(
                    "FAIL", "config_perms", f"group/other-accessible: chmod 600 {cfg.config_path}"
                )
            )
        else:
            results.append(DoctorResult("OK", "config_perms", "0600"))

    # Enrichment host, when enrichment is switched on.
    if not cfg.enrichment.enabled:
        results.append(DoctorResult("SKIP", "enrichment", "disabled"))
    else:
        from pkm_sidecar.enrichment.ollama_client import OllamaClient

        if warn_if_enrichment_endpoint_is_cleartext(cfg, logger):
            results.append(
                DoctorResult(
                    "WARN",
                    "enrichment_transport",
                    f"note bodies cross the network in cleartext to "
                    f"{scrub_url(cfg.enrichment.ollama_base_url)} (unauthenticated API) — "
                    "prefer loopback, an encrypted overlay, or an SSH tunnel",
                )
            )
        else:
            results.append(DoctorResult("OK", "enrichment_transport", "loopback or HTTPS"))

        endpoint = scrub_url(cfg.enrichment.ollama_base_url)
        ollama = OllamaClient(cfg.enrichment.ollama_base_url)
        try:
            reachable = await ollama.ping()
            results.append(
                DoctorResult(
                    "OK" if reachable else "FAIL",
                    "enrichment_reachable",
                    endpoint if reachable else f"no answer from {endpoint}",
                )
            )
            if reachable:
                # Listed separately: a host that answers but returns a malformed
                # model list is reachable. Reporting that as unreachable would
                # contradict the line directly above it.
                try:
                    present = set(await ollama.list_models())
                except OllamaError as exc:
                    results.append(
                        DoctorResult("FAIL", "enrichment_models", f"{type(exc).__name__}: {exc}")
                    )
                else:
                    wanted = {
                        "title": cfg.enrichment.title_model,
                        "tags": cfg.enrichment.tag_model,
                        "embedding": cfg.enrichment.embedding_model,
                    }
                    for role, name in wanted.items():
                        # Ollama reports "llama3.1:8b"; a bare "llama3.1" means :latest.
                        ok = name in present or f"{name}:latest" in present
                        results.append(
                            DoctorResult(
                                "OK" if ok else "FAIL",
                                f"enrichment_model_{role}",
                                name if ok else f"{name} not pulled on that host",
                            )
                        )
        finally:
            await ollama.aclose()

    # Joplin reachability + token validity.
    token = cfg.joplin.token.get_secret_value() if cfg.joplin.token else ""
    client = _make_client(cfg, token)
    try:
        reachable = await client.ping()
        results.append(
            DoctorResult("OK" if reachable else "FAIL", "joplin_reachable", cfg.joplin.base_url)
        )
        if not cfg.joplin.token:
            results.append(
                DoctorResult("SKIP", "joplin_token", "set JOPLIN_TOKEN to enable indexing")
            )
        elif reachable:
            try:
                _ = [n async for n in client.get_notes(fields=["id"], limit=1)]
                results.append(DoctorResult("OK", "joplin_token", "accepted"))
            except JoplinAuthError:
                results.append(DoctorResult("FAIL", "joplin_token", "Joplin rejected the token"))
            except JoplinError as exc:
                results.append(DoctorResult("FAIL", "joplin_token", type(exc).__name__))
        else:
            results.append(DoctorResult("SKIP", "joplin_token", "Joplin unreachable"))
    finally:
        await client.aclose()
    return results


@app.command()
def doctor(
    config: str | None = typer.Option(None, "--config"),
    db_path: str | None = typer.Option(None, "--db"),
) -> None:
    """Check config, database, Joplin connectivity, and security posture."""
    cfg = _prepare(config=config, db_path=db_path)
    results = _run_async(_run_doctor(cfg))
    any_fail = False
    for r in results:
        colour = {"OK": "green", "WARN": "yellow", "FAIL": "red", "SKIP": "yellow"}[r.status]
        typer.secho(f"{r.status}: {r.name} — {r.message}", fg=colour)
        any_fail = any_fail or r.status == "FAIL"
    raise typer.Exit(1 if any_fail else 0)


# --- open ------------------------------------------------------------------


@app.command("open")
def open_dashboard(
    config: str | None = typer.Option(None, "--config"),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
) -> None:
    """Open the dashboard in your browser with the API token handed off via URL fragment."""
    cfg = _prepare(config=config, host=host, port=port)
    if cfg.server.api_token is not None:
        token = cfg.server.api_token.get_secret_value()
    else:
        token_file = security.ephemeral_token_file_path(cfg)
        if not token_file.exists():
            typer.secho(
                "No API token found. Start the server first, or set PKM_SIDECAR_API_TOKEN.",
                fg="red",
                err=True,
            )
            raise typer.Exit(1)
        token = token_file.read_text().strip()
    url = f"http://{cfg.server.host}:{cfg.server.port}/#token={token}"
    webbrowser.open(url)
    typer.echo("Opening dashboard in your browser…")  # never print the token-bearing URL


# --- db path ---------------------------------------------------------------


@db_app.command("path")
def db_path_cmd(config: str | None = typer.Option(None, "--config")) -> None:
    """Print the absolute SQLite index path."""
    cfg = _prepare(config=config)
    typer.echo(str(cfg.database.path))
