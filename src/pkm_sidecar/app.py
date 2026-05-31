"""FastAPI application factory and lifespan (PRD §13, §14).

The lifespan owns the runtime resources: it configures logging + redaction,
resolves the API token (minting an ephemeral one if needed), bootstraps the DB,
opens the single writer connection, constructs the read-only Joplin client, and
starts the indexer background loop. Shutdown reverses all of it with a deadline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from pkm_sidecar import __version__, dashboard, db, security, services
from pkm_sidecar.api_errors import register_exception_handlers
from pkm_sidecar.api_models import HealthResponse
from pkm_sidecar.config import AppConfig
from pkm_sidecar.indexer import create_indexer
from pkm_sidecar.joplin_client import JoplinClient
from pkm_sidecar.logging_config import configure_logging, get_logger, log_event
from pkm_sidecar.views import SERVICE_NAME, router

logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: AppConfig = app.state.settings

    redact = [cfg.joplin.token.get_secret_value()] if cfg.joplin.token else []
    configure_logging(cfg.logging.level, redact_values=redact)

    resolved = security.resolve_api_token(cfg, logger)
    app.state.api_token = resolved.token
    app.state.api_token_source = resolved.source

    db.init_db(cfg.database.path)
    writer = db.open_writer_connection(cfg.database.path)
    app.state.writer = writer

    joplin_token = cfg.joplin.token.get_secret_value() if cfg.joplin.token else ""
    client = JoplinClient(
        cfg.joplin.base_url,
        joplin_token,
        timeout=10.0,
        event_poll_timeout=cfg.joplin.event_poll_seconds,
        page_limit=cfg.joplin.page_limit,
    )
    app.state.client = client
    app.state.joplin_cache = {"ts": 0.0, "reachable": False}

    indexer = create_indexer(cfg, writer, client)
    app.state.indexer = indexer
    # Only run the background loop when we can actually reach Joplin (token set)
    # and the caller asked for it (tests disable it).
    enable = app.state.start_indexer_loop and bool(cfg.joplin.token)
    # First-run backfill: serve-only (launcher) startups otherwise stay empty
    # because the loop only does incremental sync.
    if enable and services.needs_initial_rebuild(cfg, writer):
        log_event(logger, "index.full.started", reason="empty_index_backfill")
        indexer.dispatch_rebuild()
    await indexer.start(enable_background=enable)

    log_event(logger, "service.start", host=cfg.server.host, port=cfg.server.port)
    try:
        yield
    finally:
        await indexer.stop()
        await client.aclose()
        writer.close()
        if resolved.source == "ephemeral":
            security.remove_token_file(security.ephemeral_token_file_path(cfg))
        log_event(logger, "service.stop")


def create_app(settings: AppConfig, *, start_indexer_loop: bool = True) -> FastAPI:
    """Build the FastAPI app. OpenAPI docs are off by default (PRD §5 surface area)."""
    app = FastAPI(
        title=SERVICE_NAME,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.start_indexer_loop = start_indexer_loop

    register_exception_handlers(app)

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Cache-Control"] = "no-store"
        if "server" in response.headers:
            del response.headers["server"]
        return response

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        # Public, lock-free, no DB access.
        return HealthResponse(ok=True, service=SERVICE_NAME, version=__version__)

    app.include_router(router)
    app.include_router(dashboard.router)  # public GET /
    app.mount("/static", StaticFiles(directory=str(dashboard.STATIC_DIR)), name="static")
    return app
