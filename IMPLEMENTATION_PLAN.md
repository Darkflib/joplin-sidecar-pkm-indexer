# Joplin PKM Sidecar v0.1 — Implementation Plan
_Generated from PRD.md via a 13-planner fan-out → completeness critic → synthesis workflow._

**A 17-step, dependency-ordered build of the v0.1 read-only Joplin sidecar that grows the system from scaffold to launcher-integrated dashboard while keeping read-only, localhost, and no-token-leakage as hard invariants at every step.**

## Architecture at runtime
The sidecar is a Python 3.12+ package (`pkm_sidecar`) installed via uv and exposing one console script (`pkm-sidecar`, Typer-driven). At runtime, `cli.serve` resolves config (CLI > env > TOML > defaults), installs logging+token-redaction, validates the bind host as loopback, then runs a single uvicorn process hosting a FastAPI app. The app's lifespan owns three resources: (1) a single sqlite3 *writer* connection (WAL, foreign_keys=ON, busy_timeout, synchronous=NORMAL) used by the indexer; per-request *reader* connections are opened for API handlers. (2) An httpx-based `JoplinClient` that exposes only GET methods and routes through a `LocalOnlyTransport` that refuses any URL outside `JOPLIN_BASE_URL` — read-only is enforced both structurally (no mutating methods) and at runtime (transport guard). (3) An asyncio background task running `indexer.run_event_loop`, which polls `/events`, drains a bounded batch per tick, and persists the cursor only after success. A single asyncio.Lock serialises full rebuild vs incremental; rebuild dispatches via `POST /api/index/rebuild` as a tracked `asyncio.create_task` returning 202+run_id.

Data flows: `joplin_client` → `indexer` → `services` → `repositories` (SQL) → SQLite (notes, folders, tags, note_tags, extracted_tasks, extracted_links, notes_fts, index_runs, meta). `markdown_extract` is a pure leaf called by services to produce tasks/links rows. FTS5 external-content triggers maintain notes_fts on every upsert/delete. The dashboard at `GET /` is a static Jinja shell + vanilla JS that reads a bearer token from sessionStorage (seeded by URL fragment from `cli open` or launcher) and fetches `/api/*` endpoints; HTML never contains the token.

Cross-cutting: `errors.py` is the single home of the exception hierarchy (`PkmSidecarError` → Config/Auth/Database/Security/Joplin* leaves). `logging_errors.configure_logging` is the only logging setup site and installs the canonical `TokenRedactionFilter`; `security.register_secret` is a thin wrapper that adds values to that one filter. Ephemeral sidecar API tokens are minted exactly once by `security` at startup when none is configured, written to a 0600 file under the platform runtime dir, and surfaced to the dashboard via the URL-fragment handoff.

The launcher plugin (separate repo, untouched) starts the sidecar via `uv run pkm-sidecar serve …` and polls `GET /health` (unauthenticated, lock-free, <50ms). `/api/status` reports `runtime.launched_by` when `JOPLIN_SIDECAR_LAUNCHED_BY` is set. Hard invariants enforced everywhere: read-only (no PUT/PATCH/DELETE routes, no non-GET Joplin calls), localhost (bind validated, override gated behind `--allow-non-localhost`), no token leakage (SecretStr in models, redaction filter on root logger, query-string tokens forbidden), rebuildable index (delete the sqlite file and re-run rebuild reconstructs everything from Joplin).

## Sequenced build steps

Each step is self-contained and handoff-ready. Dependencies are step numbers.

### Step 1: Scaffold uv project, package layout, and CI smoke test
- **Depends on:** none  |  **Subsystems:** scaffold
- **Goal:** An installable `pkm_sidecar` package with empty stub modules for every subsystem (per PRD §6), a working `pkm-sidecar --version` console script, ruff+pytest configured, and a CI smoke test that proves `import pkm_sidecar` works.
- **Touches:** `pyproject.toml`, `.python-version`, `.gitignore`, `.pre-commit-config.yaml`, `README.md (skeleton with §23 section headings)`, `src/pkm_sidecar/__init__.py (sets __version__='0.1.0')`, `src/pkm_sidecar/__main__.py`, `src/pkm_sidecar/cli.py (stub Typer app + --version)`, `src/pkm_sidecar/{config,logging_config,app,security,joplin_client,indexer,markdown_extract,db,repositories,services,views,dashboard,models,errors,api_models,api_errors}.py (empty stubs)`, `src/pkm_sidecar/schema.sql (placeholder)`, `src/pkm_sidecar/templates/.gitkeep`, `src/pkm_sidecar/static/.gitkeep`, `src/pkm_sidecar/py.typed`, `tests/__init__.py`, `tests/conftest.py (placeholder)`, `tests/test_smoke.py`, `.github/workflows/ci.yml`
- **Acceptance:**
  - `uv sync` succeeds on a clean clone; `uv run pkm-sidecar --version` prints `0.1.0`.
  - `uv run pytest -q` passes the smoke test.
  - `uv run ruff check .` passes with the opinionated rule set (E,F,I,B,UP,SIM,RUF).
  - Wheel build (`uv build`) includes schema.sql, templates/, static/ as package data.

### Step 2: Errors module and exception hierarchy (single source of truth)
- **Depends on:** 1  |  **Subsystems:** logging_errors, joplin_client, db, security, api, indexer
- **Goal:** Replace the stub `errors.py` with the canonical, single PkmSidecarError hierarchy used by every other subsystem. Resolves the critique's exception-naming conflict by picking one set of names.
- **Touches:** `src/pkm_sidecar/errors.py`, `tests/unit/test_errors.py`
- **Acceptance:**
  - Defines: PkmSidecarError; ConfigError; SecurityError; AuthError (api 401); DatabaseError, DatabaseLockedError, SchemaVersionMismatchError, FTSUnavailableError; JoplinError, JoplinUnreachableError, JoplinAuthError, JoplinNotFoundError, JoplinRateLimitedError, JoplinCursorInvalidError, JoplinBadResponseError; IndexInProgressError, NotFoundError, ConflictError.
  - Provides `to_http_status(exc) -> int` mapping that the api layer will register: AuthError→401, NotFoundError→404, ConflictError→409, JoplinAuthError→502, JoplinUnreachableError→503, DatabaseLockedError→503, IndexInProgressError→409, others→500.
  - JoplinError.__init__ scrubs `token=` from any URL it stores so a stack trace alone cannot leak the token.
  - Unit test asserts the URL-scrubbing and the to_http_status table.

### Step 3: Config loader with Pydantic v2, SecretStr, and CLI>env>TOML>defaults precedence
- **Depends on:** 1, 2  |  **Subsystems:** config
- **Goal:** A single immutable AppConfig produced by `load_config(*, config_path, cli_overrides, env)` that handles all of PRD §7. Joplin and sidecar API tokens are SecretStr. Localhost-bind validation lives in security (called from here for early failure), but the bind-string rule and `--allow-non-localhost` semantics are honoured. Missing Joplin token warns at load, errors only when indexing operations are invoked via `require_joplin_token(cfg)`.
- **Touches:** `src/pkm_sidecar/config.py`, `tests/unit/test_config.py`
- **Acceptance:**
  - All ten env vars in PRD §7.1 are honoured; empty string treated as unset for tokens/URLs; empty string for port raises.
  - TOML loader maps [server], [joplin], [database], [logging], [indexing] sections to the corresponding nested models.
  - Precedence test (CLI overlay None==unset) passes for each field.
  - AppConfig and all nested models are frozen=True; SecretStr fields redact in repr/model_dump.
  - `~` expansion on db_path and config_path; mkdir parents=True on data and config dirs.
  - `require_joplin_token(cfg)` raises ConfigError when token missing; load_config succeeds with warning.

### Step 4: Logging configuration with canonical TokenRedactionFilter
- **Depends on:** 1, 2, 3  |  **Subsystems:** logging_errors
- **Goal:** One logging setup function that installs the canonical filter on the root logger so every handler (uvicorn, httpx, app) redacts registered secrets. Resolves the critique's two-filter divergence by making logging_errors the owner; security will later call into this module to register additional secrets.
- **Touches:** `src/pkm_sidecar/logging_config.py`, `tests/unit/test_logging_errors.py`, `tests/unit/test_redaction.py`
- **Acceptance:**
  - `configure_logging(level, redact_values=(...))` is idempotent and installs the filter on the root logger and on the `pkm_sidecar.*` tree before any HTTP/SQL client is constructed.
  - Format matches PRD §16: `ISO8601 LEVEL logger.name message k=v …`. Trailing fields shlex-quoted.
  - `get_logger(name)` returns `logging.getLogger(f'pkm_sidecar.{name}')`.
  - `log_event(logger, event, **fields)` emits the §16 event names; refuses fields named body/token/authorization/password.
  - TokenRedactionFilter rewrites secrets in record.msg, record.args, exc_info text, and string attributes in record.__dict__. Length-cap MaxLineLengthFilter (2 KiB) attached.
  - `add_secret(value)` mutates the singleton filter so security can register the ephemeral token at startup.
  - Async `retry_with_backoff(coro_fn, *, retries, base_delay, max_delay, jitter, retry_on, logger, op)` and `LogOnceGuard` are exposed.
  - Test asserts the configured Joplin token literal never appears in any caplog record across a forced exception path.

### Step 5: Security primitives: bind validation, ephemeral token, bearer dep, LocalOnlyTransport
- **Depends on:** 2, 3, 4  |  **Subsystems:** security
- **Goal:** All cross-cutting security primitives live in `security.py`. Owns: bind-loopback allowlist {127.0.0.1, ::1, localhost} with `--allow-non-localhost` opt-in; ephemeral token generation and writing to a 0600 file under the platform runtime dir (macOS: ~/Library/Application Support/pkm-sidecar; Linux: $XDG_RUNTIME_DIR or ~/.cache/pkm-sidecar; Windows: %LOCALAPPDATA%); FastAPI bearer dependency using hmac.compare_digest; LocalOnlyTransport (httpx.AsyncBaseTransport) that raises SecurityError on any (scheme,host,port) outside JOPLIN_BASE_URL; `warn_if_world_readable_config(path)`; `register_secret(value)` thin wrapper calling logging_config.add_secret.
- **Touches:** `src/pkm_sidecar/security.py`, `tests/unit/test_security.py`
- **Acceptance:**
  - `validate_bind_address(host, allow_non_localhost)` accepts 127.0.0.1/::1/localhost; rejects 0.0.0.0/192.168.x with SecurityError unless override.
  - `resolve_api_token(config, logger)` returns a ResolvedToken with source 'config'|'env'|'ephemeral'. Ephemeral path mints `secrets.token_urlsafe(32)`, writes atomically with chmod 0600 (POSIX) to deterministic path, logs the path only (not the value), and registers the value with the redaction filter.
  - `require_bearer_token(settings)` dependency: 401 'missing bearer token' if absent, 401 'invalid bearer token' if mismatch (constant-time compare); never echoes the offered token.
  - `LocalOnlyTransport` raises SecurityError when handed an httpx.Request whose URL parts differ from configured Joplin base. Unit test fires a request to https://evil.example/ and asserts the raise.
  - `warn_if_world_readable_config` emits one WARNING with chmod hint on POSIX when mode & 0o077 != 0; skipped on Windows.
  - Override + non-loopback host requires a non-ephemeral configured token (refuse to start otherwise).

### Step 6: Database layer: schema.sql, connection model, FTS, meta, transaction helpers
- **Depends on:** 2, 3, 4  |  **Subsystems:** db
- **Goal:** Author the canonical schema and the boring sqlite3 driver. Resolves cross-cutting decisions: synchronous=NORMAL added to PRAGMAs; FK notes.parent_id REFERENCES folders(id) ON DELETE SET NULL; reset_derived does NOT delete the notes table (rebuild upserts notes and marks unseen as deleted to preserve rowids for FTS); per-request reader connections + dedicated writer connection (no shared global).
- **Touches:** `src/pkm_sidecar/schema.sql`, `src/pkm_sidecar/db.py`, `tests/unit/test_db_schema.py`
- **Acceptance:**
  - schema.sql is idempotent (CREATE … IF NOT EXISTS for everything) and includes: meta, folders, notes, tags, note_tags, extracted_tasks, extracted_links, index_runs, notes_fts (external-content), notes_ai/notes_ad/notes_au triggers per PRD §9 plus the indexes on notes(parent_id,updated_time,is_todo,deleted), note_tags(tag_id), extracted_*(note_id), folders(parent_id).
  - FK on notes.parent_id is ON DELETE SET NULL.
  - `open_writer_connection(path)` and `open_reader_connection(path)` both apply: journal_mode=WAL, foreign_keys=ON, busy_timeout=5000, synchronous=NORMAL, row_factory=sqlite3.Row. Writer is single (owned by indexer); readers are short-lived per API request.
  - `init_db(path)` is idempotent and creates parent dir with 0700 perms.
  - `transaction(conn)` is a BEGIN IMMEDIATE context manager with retry/backoff (3 attempts, 50/200/500ms) on `database is locked`.
  - `get_meta/set_meta`, `fts_escape`, `fts_available` probe, `reset_derived` (wipes extracted_tasks, extracted_links, note_tags; clears related meta keys; preserves notes/folders/tags so rebuild can upsert).
  - Schema-version meta key written at first init; mismatch raises SchemaVersionMismatchError.
  - Tests cover: idempotent init; PRAGMAs active; FTS triggers fire on insert/update/delete; lock retry succeeds when a competing BEGIN IMMEDIATE releases; reset_derived preserves notes.

### Step 7: Pydantic models + repositories (SQL-only, idempotent upserts, FTS-safe search)
- **Depends on:** 6  |  **Subsystems:** repositories
- **Goal:** All SQL lives behind a typed API. Resolves the FTS-escape duplication by importing `db.fts_escape`; uses sqlite3 sync; returns Pydantic models for reads, accepts dicts for writes. body_hash short-circuit returns body_changed bool based on hash only.
- **Touches:** `src/pkm_sidecar/models.py`, `src/pkm_sidecar/repositories.py`, `tests/unit/test_models.py`, `tests/unit/test_repositories.py`, `tests/unit/test_fts_query.py`
- **Acceptance:**
  - models.py defines FolderRow, NoteRow, NoteSummary (no body field — defence in depth), TagRow, ExtractedTask, ExtractedLink (link_type Literal), SearchHit, StatusCounts.
  - NoteRepository methods per the repositories subsystem contract: upsert_folder/note/tag/note_tag, replace_tasks_for_note, replace_links_for_note, mark_note_deleted (sets deleted=1 + clears FTS), mark_folder/tag_deleted, sweep_orphans(run_started_at), get_note, get_tasks_for_note, get_links_for_note, fetch_recent, fetch_inbox (case-insensitive), fetch_untagged, fetch_todos (is_todo=1 AND not completed UNION notes with unchecked extracted_tasks), fetch_stale (user_updated_time fallback updated_time), fts_search (uses db.fts_escape, returns FTS5 snippet()), get_status_counts, get_meta/set_meta.
  - All fetch_* exclude deleted=1 with no opt-in.
  - body_hash is sha256 hex of body utf-8; upsert_note returns True iff body_hash changed.
  - FTS escape: tokenise on whitespace, escape embedded `"` as `""`, wrap each token as quoted phrase, join with space (implicit AND); empty/operator-only queries return empty list.
  - Tests cover all decisions including the 'snippet present' assertion and the 'body field absent from NoteSummary JSON' assertion.

### Step 8: Markdown extractor (pure, line-oriented, code-fence aware)
- **Depends on:** 1, 7  |  **Subsystems:** markdown_extract
- **Goal:** Pure `extract(note_id, body) -> {tasks, links}` with compiled module-level regexes. Resolves ambiguities: accept `x` and `X` as checked; skip ``` and ~~~ fenced blocks; strip backtick spans before link extraction; internal_joplin = `:/<32hex>` exact; 1-based line numbers; emit both task and link rows when a task line contains a link.
- **Touches:** `src/pkm_sidecar/markdown_extract.py`, `tests/unit/test_markdown_extract.py`, `tests/fixtures/notes/with_tasks.md`, `tests/fixtures/notes/with_links.md`, `tests/fixtures/notes/edge_cases.md`
- **Acceptance:**
  - Returns shape contracts exactly matching repositories.ExtractedTask / ExtractedLink.
  - Matrix tests cover -/*/+ bullets, [ ]/[x]/[X], indented/nested bullets, CRLF, code-fenced false positives suppressed.
  - Link classifier returns one of {internal_joplin, external_url, relative, other}; mailto:/tel: fall to other.
  - Image syntax `![alt](src)` matches as a link with link_text starting `!` (documented).
  - Reference-style and wikilinks explicitly NOT extracted in v0.1 (documented in module docstring).

### Step 9: Joplin client: GET-only async client with LocalOnlyTransport and typed errors
- **Depends on:** 2, 3, 4, 5  |  **Subsystems:** joplin_client
- **Goal:** JoplinClient class with only get_* methods, async-iterator pagination, GET-only transport assertion, token in `?token=` query (scrubbed by redact filter), httpx.Timeout(connect=5, read=10), retry policy via logging_errors.retry_with_backoff on transient errors only, typed exceptions from errors.py.
- **Touches:** `src/pkm_sidecar/joplin_client.py`, `tests/unit/test_joplin_client_pagination.py`, `tests/unit/test_joplin_client_readonly.py`, `tests/fixtures/joplin/notes_page1.json`, `tests/fixtures/joplin/notes_page2.json`, `tests/fixtures/joplin/folders.json`, `tests/fixtures/joplin/tags.json`, `tests/fixtures/joplin/events_page.json`, `pyproject.toml (add respx, pytest-httpx as dev dep)`
- **Acceptance:**
  - Constructor builds httpx.AsyncClient wrapping security.LocalOnlyTransport so non-Joplin URLs raise SecurityError at runtime.
  - Only public methods: aclose, __aenter__/__aexit__, ping (non-raising bool), get_note, get_notes, get_folders, get_tags, get_tag_notes, get_events. AST scan test asserts no method calls httpx with method != 'GET'.
  - ALLOWED_PATHS frozenset; internal _request asserts method=='GET' and path in allowlist.
  - Pagination iterator follows page/has_more, caps loops at 10_000 pages defensively.
  - Maps 404→JoplinNotFoundError, 401/403→JoplinAuthError, ConnectError/Timeout→JoplinUnreachableError, 429/Retry-After→JoplinRateLimitedError, events cursor-too-old→JoplinCursorInvalidError, other non-2xx→JoplinBadResponseError. All store a token-scrubbed URL.
  - ping() returns bool, swallows errors, uses LogOnceGuard so unreachable/auth states don't spam logs.
  - get_events(cursor, limit) returns {items, cursor, has_more}; caller (indexer) owns cursor persistence.
  - Pagination loop terminates correctly on real fixtures; auth/unreachable/cursor-invalid map to typed exceptions.
  - Test asserts the literal token never appears in any caplog record or exception repr across error paths.

### Step 10: Services layer + indexer: full rebuild, incremental sync, reindex_note, background loop
- **Depends on:** 4, 5, 6, 7, 8, 9  |  **Subsystems:** indexer, repositories
- **Goal:** Orchestration that converts Joplin payloads into derived rows. Single reindex_note primitive; module-level asyncio.Lock serialises rebuild vs incremental; rebuild upserts notes/folders/tags then marks unseen as deleted (preserves rowids); incremental drains a bounded batch per tick and advances cursor only on success; event-cursor-invalid demotes to full rebuild via the same lock release-then-reacquire; background loop driven by `asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)`.
- **Touches:** `src/pkm_sidecar/services.py`, `src/pkm_sidecar/indexer.py`, `tests/unit/test_events_processing.py`, `tests/integration/test_full_rebuild.py`, `tests/integration/test_incremental_sync.py`
- **Acceptance:**
  - full_rebuild: fetch folders → notes (paginated batches, one transaction per page) → tags → tag-notes; upsert each entity with indexed_at=now; sweep_orphans(run_started_at) marks notes/folders/tags absent from this run as deleted (notes: soft; folders/tags: hard). Sets joplin.last_full_index_at. Writes an index_runs row (run_id, mode, status, counts, message).
  - incremental_sync_once: read joplin.last_event_id from meta → client.get_events → dispatch per event → advance cursor only after the whole batch succeeds → set joplin.last_incremental_index_at. Bounded at max 1000 events per tick (configurable).
  - reindex_note(conn, client, note_id): fetch canonical field list, compute body_hash, upsert via repositories.upsert_note. If body_changed, call markdown_extract.extract and repositories.replace_tasks_for_note / replace_links_for_note. Replace note_tags for this note. All in one BEGIN IMMEDIATE transaction.
  - Note deletion event sets notes.deleted=1 idempotently (no-op when row absent).
  - Tag/folder events refresh that single row; cross-note tag-membership drift is deferred to next full rebuild (documented).
  - EventCursorInvalidError handler: clear meta cursor, run full_rebuild, set index_runs message='cursor invalid → rebuilt', surface in /api/status.last_error.
  - IndexerHandle exposes start(enable_background: bool = True), stop() (sets stop_event, awaits task with 3s timeout for launcher SIGTERM compliance), status(); test mode disables background loop.
  - Background loop catches broad Exception per tick, logs with stack, continues; only stop_event terminates it.
  - POST-style entry points: run_full_rebuild() and run_incremental_once() are awaitable from the API layer.
  - Integration tests against respx-mocked Joplin and tmp sqlite cover end-to-end full rebuild and incremental update with body change.

### Step 11: API: FastAPI app factory, /health, /api/status, exception handlers
- **Depends on:** 5, 7, 10  |  **Subsystems:** api
- **Goal:** Minimal app surface that the launcher's health probe can rely on while the rest of the API grows. /health is constant-time and lock-free; /api/status aggregates with a 5s cached Joplin reachability probe; exception handlers map PkmSidecarError → ErrorEnvelope without leaking secrets.
- **Touches:** `src/pkm_sidecar/app.py`, `src/pkm_sidecar/api_models.py`, `src/pkm_sidecar/api_errors.py`, `src/pkm_sidecar/views.py (status + health initial)`, `tests/integration/test_api_public.py`, `tests/integration/test_api_status_health.py`
- **Acceptance:**
  - `create_app(settings) -> FastAPI` builds the app with docs_url=None, redoc_url=None, openapi_url=None by default (security default-off; opt-in flag deferred).
  - Lifespan: configure_logging → register_secret for joplin + api tokens → init_db → open writer connection → construct JoplinClient with LocalOnlyTransport → start indexer background task (driven by config flag, default on in serve, off in tests) → on shutdown, indexer.stop() with 3s deadline, close JoplinClient, close writer connection, cleanup_token_file.
  - GET /health → 200 {ok, service:'pkm-sidecar', version} from `pkm_sidecar.__version__`; public; no DB touch; <50ms; <2000ms even during full rebuild.
  - GET /api/status → StatusResponse: service, version, joplin{configured, reachable, base_url, last_error}, database{path, counts via repositories.get_status_counts}, indexing{last_full_index_at, last_incremental_index_at, last_event_id}, runtime{launched_by from JOPLIN_SIDECAR_LAUNCHED_BY env}. Joplin reachability is cached 5s. Base URL is redacted via security.redact_token before serialisation. Token literal absent from response (string-not-in assertion).
  - Error envelope: `{"error":{"code":str,"message":str}}`; catch-all handler logs exception but does not include exc args in body.
  - Middleware adds X-Content-Type-Options: nosniff, Cache-Control: no-store on /api responses; strips Server header.
  - Tests confirm /health works without auth and /api/status is auth-gated (after step 12).

### Step 12: API: protected /api/* routes, bearer auth, workflow + search views, index commands
- **Depends on:** 11  |  **Subsystems:** api, security, indexer
- **Goal:** Add every PRD §13.2 protected endpoint behind security.require_bearer_token. Rebuild dispatched via app.state.RebuildManager that wraps asyncio.create_task (NOT FastAPI BackgroundTasks — must start before response).
- **Touches:** `src/pkm_sidecar/views.py`, `src/pkm_sidecar/app.py (mount /api router with router-level auth Depends)`, `tests/integration/test_api_auth.py`, `tests/integration/test_api_views.py`, `tests/integration/test_api_search.py`, `tests/integration/test_reindex_endpoint.py`
- **Acceptance:**
  - Router prefix='/api' with `dependencies=[Depends(require_bearer_token)]` on the router; / and /health and /static/* remain unauthenticated.
  - GET /api/notes/recent, /inbox, /untagged, /todos, /stale: shared Pagination dependency (limit 1..500, offset 0..10000); call repositories.fetch_*. Inbox uses settings.indexing.inbox_folder_names.
  - GET /api/search?q=…: q required min_length=1 after strip (422 otherwise); calls repositories.fts_search.
  - GET /api/note/{note_id} → NoteDetail INCLUDING body (the only endpoint exposing body); /tasks and /links sub-routes return lists.
  - POST /api/index/rebuild → 202 {run_id, mode:'full', started_at}; 409 IndexInProgressError if rebuild already running. Work dispatched via RebuildManager (asyncio.create_task with done-callback that records index_runs failure on exception).
  - POST /api/index/sync → 202 {accepted:true} dispatching indexer.run_incremental_once via asyncio.create_task.
  - POST /api/index/note/{note_id} → 202 {accepted, note_id}; 404 if Joplin returns not-found.
  - Parametrised auth test asserts every /api/* route returns 401 with no/invalid bearer and the expected 200/202 with valid bearer.
  - OpenAPI introspection test asserts schema exposes no PUT/PATCH/DELETE and exactly the three documented POSTs.

### Step 13: Dashboard: Jinja2 shell + vanilla JS with URL-fragment token handoff
- **Depends on:** 11, 12  |  **Subsystems:** dashboard
- **Goal:** Static HTML shell at GET / with /static/* assets; JS reads `#token=...` from URL fragment on load, writes to sessionStorage, immediately strips via history.replaceState; falls back to a paste banner. Degraded badge when /api/status reports joplin.reachable=false.
- **Touches:** `src/pkm_sidecar/dashboard.py`, `src/pkm_sidecar/templates/base.html`, `src/pkm_sidecar/templates/index.html`, `src/pkm_sidecar/static/dashboard.css`, `src/pkm_sidecar/static/dashboard.js`, `src/pkm_sidecar/app.py (mount static, include dashboard router)`, `tests/integration/test_dashboard.py`, `tests/integration/test_degraded_mode.py`
- **Acceptance:**
  - GET / returns 200 HTML; public; response body contains neither Joplin nor sidecar API token literals.
  - /static/dashboard.js parses URL fragment, sessionStorages the token, history.replaceState strips it, attaches Authorization: Bearer to fetch() calls.
  - Sections rendered: status, recent, inbox, untagged, todos, stale, search. Search form intercepted by JS.
  - On /api/status with joplin.reachable=false, JS paints 'Joplin offline — showing stale index' badge while populating sections from local data.
  - CSP header: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`.
  - Asset URL is version-stamped: `/static/dashboard.js?v={version}` to bust caches on upgrade.

### Step 14: CLI: serve, doctor, status, index rebuild/sync, db path, open
- **Depends on:** 3, 4, 5, 6, 9, 10, 11, 12, 13  |  **Subsystems:** cli
- **Goal:** Six commands wired to subsystems. `serve` runs uvicorn.Server programmatically on the same event loop that hosts the indexer background task. `doctor` is a registry of DoctorCheck objects with stable names. `open` (added as an explicit UX convenience) opens the dashboard URL `http://host:port/#token=…` in the default browser to satisfy the dashboard handoff contract.
- **Touches:** `src/pkm_sidecar/cli.py`, `tests/integration/test_cli_smoke.py`
- **Acceptance:**
  - Typer app with sub-Typers `index` and `db`. Console script `pkm-sidecar`.
  - Every command calls (1) logging_config.configure_logging first, (2) load_config with cli_overrides, (3) security checks, before doing work.
  - `serve`: calls security.validate_bind_address before bind; runs `uvicorn.Server(uvicorn.Config(app, host, port, log_config=None)).serve()` via asyncio.run; emits service.start / service.stop log events; clean SIGINT exit 130; SIGTERM-triggered graceful shutdown within 3 s.
  - `index rebuild` / `index sync`: in-process; print summary counts; exit 0 on success, 1 Joplin unreachable, 2 config error, 4 partial errors, 5 cursor invalid.
  - `status`: prints human table by default; `--json` matches /api/status payload exactly; token never printed.
  - `doctor`: registry includes config readable, db dir writable, FTS5 available (db.fts_available probe), Joplin /ping reachable, Joplin token valid (skip if unset), bind address local, config file perms (POSIX). Stable line prefix OK:/FAIL:/SKIP: for assertions.
  - `db path`: prints the absolute SQLite path only.
  - `open`: constructs `http://{host}:{port}/#token={resolved_token}` and opens via webbrowser.open. Token is read from disk (ephemeral file) or config.
  - Exit code table documented in --help epilog and README.

### Step 15: Comprehensive test pass: integration acceptance + no-mutation guard + manual checklist
- **Depends on:** 1, 8, 9, 10, 11, 12, 13, 14  |  **Subsystems:** testing
- **Goal:** Fill out the testing suite so every PRD §18 criterion has a named test and the no-mutation invariant is enforced at integration level via a respx autouse fixture. Coverage gates set to 85% on core modules, 70% overall.
- **Touches:** `tests/conftest.py (full fixture set: tmp_db_path, sqlite_conn, settings, api_token, fake_joplin, joplin_client, app with dependency_overrides, client, authed_client, freezegun helpers, caplog secret-asserter)`, `tests/integration/test_no_mutation.py`, `tests/integration/test_auth_enforced.py`, `tests/manual/CHECKLIST.md (single consolidated manual checklist — see decisions)`, `pyproject.toml (add freezegun, pytest-cov; configure markers, asyncio_mode=auto, coverage gate)`, `.github/workflows/test.yml (run `uv run pytest -m 'not manual' --cov=pkm_sidecar --cov-fail-under=70`)`
- **Acceptance:**
  - All ten PRD §18 acceptance criteria have at least one named test (mapping in tests/README.md): 18.1 health/serve, 18.2 doctor, 18.3 full rebuild, 18.4 search, 18.5 recent, 18.6 untagged AND inbox (explicit inbox assertion to close DoD gap), 18.7 task extraction, 18.8 incremental sync, 18.9 degraded mode, 18.10 no mutation (respx autouse + AST scan).
  - respx autouse fixture asserts request.method == 'GET' on every intercepted Joplin call in the integration suite.
  - AST grep test asserts `import httpx` appears only in joplin_client.py.
  - CI run: `uv run pytest -m 'not manual'` green; coverage thresholds met.
  - Single tests/manual/CHECKLIST.md merges manual scenarios for sidecar correctness AND launcher smoke test (resolves the two-checklist critique).

### Step 16: README and launcher integration documentation
- **Depends on:** 14, 15  |  **Subsystems:** launcher_integration
- **Goal:** Finalise PRD §23 README sections plus the launcher integration contract: canonical Command/Arguments/Health URL triple, three documented secret-injection paths, autostart guidance, first-run warning, security model, limitations, doctor, rebuild, security model, v0.1 non-goals. No launcher code changes.
- **Touches:** `README.md`, `docs/launcher.md`
- **Acceptance:**
  - README has the section list from PRD §23 filled in with working commands.
  - Documents PKM_SIDECAR_CONFIG_PATH (~/.config/pkm-sidecar/config.toml mode 0600) as primary token source; wrapper script as alternative; login-shell env as fragile footnote.
  - Documents that the ephemeral sidecar API token file path is logged once at startup and consumed by `pkm-sidecar open` and the launcher.
  - Recommended launcher settings: Command absolute path to `uv`, Arguments `["run","--project","<repo>","pkm-sidecar","serve","--host","127.0.0.1","--port","8765"]`, Health URL `http://127.0.0.1:8765/health`. Alternative `uv tool run` form.
  - Warning: first run from terminal before enabling launcher autostart; sidecar tested against joplin-sidecar-launcher v0.1.1.
  - Documents Windows-under-launcher as experimental and notes 0o600 limitation on Windows.

### Step 17: Definition-of-Done verification + manual sign-off
- **Depends on:** 16  |  **Subsystems:** testing, launcher_integration
- **Goal:** Walk the PRD §24 checklist end-to-end against a real Joplin instance and confirm every item passes. Capture results in tests/manual/CHECKLIST.md run notes; tag v0.1.0.
- **Touches:** `tests/manual/CHECKLIST.md (filled with pass/fail run notes)`, `CHANGELOG.md (v0.1.0 entry)`
- **Acceptance:**
  - Each PRD §24 criterion has a checked box and a one-line evidence note.
  - Tag v0.1.0 cut on the commit that has all checks green.
  - OpenAPI surface (introspection at test time, since docs are disabled) confirmed no PUT/PATCH/DELETE.

## Critical decisions (resolved, with rationale)

**Who owns generation of the ephemeral sidecar API token and how does the dashboard learn it?**
- **Decision:** security.py owns generation at app startup (during FastAPI lifespan, after config load). Token is written to a 0600 file at a platform-specific runtime dir, registered with the canonical TokenRedactionFilter, and surfaced to the dashboard via a `pkm-sidecar open` CLI command (and the launcher) that constructs `http://host:port/#token=<value>`. config.py only flags `api_token_was_generated` for status reporting; it does NOT mint the token.
- **Why:** Three plans assumed they owned this. Concentrating in security gives one source for redaction-filter registration, one source for file lifecycle (atexit + lifespan shutdown cleanup), and a single contract (`security.ephemeral_token_file_path(config)`) that CLI and launcher both consume.

**Full-rebuild semantics for the notes table — DELETE or upsert+mark-unseen?**
- **Decision:** Upsert each note and mark unseen rows as deleted=1 via sweep_orphans(run_started_at). reset_derived ONLY wipes extracted_tasks, extracted_links, note_tags.
- **Why:** FTS5 external-content tables reference notes by rowid; DELETE FROM notes renumbers rowids on the next INSERT and breaks the FTS linkage. Preserving rowids is correct; upsert is also faster on re-rebuild because most rows are unchanged.

**Connection model: shared singleton with check_same_thread=False vs per-request reader connections plus dedicated writer?**
- **Decision:** Dedicated writer connection owned by the indexer background task; short-lived reader connections opened per API request via a FastAPI dependency.
- **Why:** WAL allows one writer + many readers concurrently. Per-request readers eliminate the need for a global write lock around reads and avoid the check_same_thread=False footgun under uvicorn workers. The writer connection's queue is naturally serialised because there's exactly one writer task.

**Canonical exception class names (Joplin* hierarchy)**
- **Decision:** JoplinError → JoplinUnreachableError, JoplinAuthError, JoplinNotFoundError, JoplinRateLimitedError, JoplinCursorInvalidError, JoplinBadResponseError. All defined ONCE in errors.py; joplin_client, indexer, logging_errors all import from there.
- **Why:** Resolves the joplin_client/logging_errors naming clash (`JoplinUnreachable` vs `JoplinUnreachableError`, `JoplinCursorInvalid` vs `JoplinEventCursorError`). Consistent *Error suffix follows Python stdlib convention and removes ambiguity in `except` clauses.

**Token-redaction filter ownership**
- **Decision:** logging_errors owns the TokenRedactionFilter implementation. security.register_secret(value) is a one-line wrapper that calls logging_config.add_secret(value).
- **Why:** Two parallel filters would either drift or one would silently lose secrets. Single canonical filter installed on the root logger before any subsystem instantiates an httpx client.

**FK behaviour on notes.parent_id REFERENCES folders(id)**
- **Decision:** ON DELETE SET NULL.
- **Why:** Joplin can delete a folder while notes still reference it (and event ordering is not guaranteed). SET NULL keeps the note row alive and orphan'd rather than failing the indexer; the dashboard already needs to handle notes without a parent.

**Resource/attachment 'basic placeholder fields' (PRD §3.1/§3.2)**
- **Decision:** Interpret minimally for v0.1: only the existing notes.source_url column. No resources table, no /api/resources endpoint. Document this explicitly in README §limitations.
- **Why:** PRD §3.2 lists 'attachment/resource indexing beyond basic placeholder fields' as out of scope. source_url on notes is the simplest 'placeholder field' interpretation that needs no new schema. A resources stub adds risk without a user-visible v0.1 view that consumes it.

**Workflow view 'notes likely requiring review' (PRD §4.6)**
- **Decision:** Drop from v0.1 scope; document under README limitations as 'planned for v0.2'. Do not add a fetch_review repository or endpoint.
- **Why:** Heuristic is undefined in PRD; building it without a clear definition risks shipping the wrong thing. The other five views (recent/inbox/untagged/todos/stale) already cover the bulk of the workflow value. Defer until v0.2 with a real heuristic spec.

**Body-hash change-detection: hash-only or hash + updated_time?**
- **Decision:** Hash-only. upsert_note returns body_changed solely on body_hash diff; updated_time is only used downstream for ordering.
- **Why:** If the hash changed, derived state is stale regardless of updated_time. If hash unchanged, nothing needs refreshing. Adding an updated_time gate creates a corner case where a clock-skew or sync-conflict edit is missed.

**Rebuild dispatch: BackgroundTasks vs asyncio.create_task?**
- **Decision:** asyncio.create_task tracked in app.state.RebuildManager with a done-callback that records index_runs failure on exception. Endpoint returns 202 + run_id immediately.
- **Why:** FastAPI BackgroundTasks fire AFTER the response is flushed, so a quick follow-up /api/status call could miss the in-progress state. create_task starts work immediately and is observable from app.state for the in-progress guard.

**OpenAPI docs default state**
- **Decision:** Default OFF (docs_url=None, redoc_url=None, openapi_url=None). Tests use FastAPI internals to introspect routes; no user-facing /docs in v0.1.
- **Why:** Localhost-only service; reducing surface area aligns with PRD §5 design principles. Trivially flippable later when there's a real consumer (v0.2 launcher panel, perhaps).

**Default paths on macOS**
- **Decision:** Data (DB) follows PRD literal (~/.local/share/pkm-sidecar/) for consistency across platforms; runtime files (ephemeral token) use ~/Library/Application Support/pkm-sidecar/ on macOS because that's the platform's correct location for runtime app data.
- **Why:** The PRD pins data paths explicitly so violating that needs justification; runtime/secret files are NOT pinned by the PRD and macOS users expect them in Application Support. Document the split in README.

**Dashboard auth token handoff**
- **Decision:** URL-fragment + sessionStorage as the primary path, manual paste banner as fallback. Bearer header only on /api/* (no query-string token, no cookies).
- **Why:** Fragments are never sent to servers, never logged, never appear in Referer. sessionStorage scope-dies with the tab. Avoids the query-string-in-access-logs leak and the cookie/CSRF surface.

**CLI startup logging of the dashboard URL**
- **Decision:** DO NOT print the `http://…/#token=…` URL to stdout/logs. Provide `pkm-sidecar open` (and the launcher 'Open Dashboard' button) as the only paths that materialise it.
- **Why:** Launcher captures stdout to an in-memory buffer that is copyable via 'Copy diag'. Logging the token-bearing URL would leak the token to clipboard. The `open` command builds it transiently for browser handoff only.

**Single vs split manual checklist**
- **Decision:** Consolidate into one tests/manual/CHECKLIST.md with two sections: 'Sidecar-only acceptance' and 'Launcher integration smoke'. Delete the duplicate scripts/launcher-smoke-test.md.
- **Why:** Two checklists invite drift. Single location with clearly labelled sections satisfies both PRD §19.3 and §24 launcher sign-off needs.

**Cross-note tag-membership drift in incremental sync**
- **Decision:** Accept for v0.1: refresh /notes/:id/tags on note-touched events; refresh tag row on tag-touched events; defer cross-note tag-membership changes to next full rebuild. Document this behaviour in README §limitations and surface 'tag membership may be stale; rebuild to refresh' in the dashboard tag view if/when added.
- **Why:** Joplin's event surface for note↔tag link changes is awkward and the alternative (refresh all notes for a tag on every tag event) doubles request volume against Joplin. v0.1 acceptance criteria do not require perfect tag freshness.

## Open questions for the user

These have a recommended default (applied unless you say otherwise):
1. Should the launcher v0.1.1 be updated to read the sidecar ephemeral token file and inject the Authorization header when proxying API calls from its panel? Out of scope for this sidecar v0.1 build, but blocks 'panel-driven dashboard' UX. Recommend filing as a follow-up issue on joplin-sidecar-launcher.
2. Is Windows in v0.1 support scope? launcher_integration plan documents Windows-under-launcher as experimental; 0o600 perms are a no-op there. Need an explicit policy: 'macOS + Linux supported, Windows best-effort' or 'all three supported, document gaps'.
3. Acceptable behaviour when the user sets --allow-non-localhost AND no token: security plan recommends 'refuse to start'. Confirm — this changes a 'warn' decision in config into a 'fail' decision in security and we want the synthesizer's blessing before locking it in.
4. Maximum events per incremental tick: indexer plan recommends 1000 with the remainder processed next tick. Confirm 1000 is acceptable, or pick a different bound (config-exposed).
5. Does v0.1 ship the optional GET /api/note/{id}/tasks and /links endpoints (PRD §13.2 lists them as optional)? Plan currently includes them; confirm or drop.

## Definition-of-Done coverage (PRD §24)

| Done criterion | Satisfied by step(s) |
|---|---|
| package installs/runs locally | 1, 14, 16 |
| pkm-sidecar doctor works | 14, 15 |
| pkm-sidecar index rebuild indexes real Joplin data | 10, 14, 17 |
| pkm-sidecar serve starts a local API | 11, 14 |
| /health works without auth | 11, 15 |
| /api/status works with auth | 11, 12, 15 |
| /api/search returns FTS results | 7, 12, 15 |
| recent, inbox, untagged, TODO, and stale views work | 7, 12, 15 |
| incremental sync updates changed notes | 10, 15 |
| service tolerates Joplin being temporarily unavailable | 9, 10, 11, 13, 15 |
| tests pass | 15, 17 |
| README documents setup, risks, and limitations | 16 |
| no Joplin mutation endpoints exist | 9, 12, 15 |

## Risks
- Joplin /events endpoint shape is under-documented in the PRD. The indexer's cursor-invalid recovery path and event dispatch depend on assumptions that must be confirmed against a live Joplin during step 9/10. Mitigation: tests/manual/CHECKLIST.md explicitly walks an event-driven update against real Joplin.
- SIGTERM graceful shutdown within 3s (launcher's SIGKILL deadline) requires the indexer's background loop to yield between batches and the writer connection's transaction to commit/abort cleanly. Risk: an in-flight per-page rebuild transaction holds the lock past the deadline. Mitigation: per-batch transactions are small (one page = ≤100 notes); stop_event is checked between batches.
- FTS5 may be missing from some Python sqlite3 builds. doctor probes via fts_available and tests xfail with a clear message; first-run users on niche distros may need to install a newer Python.
- Token-redaction filter relies on the configured token values being registered BEFORE any HTTP/SQL log line is emitted. If lifespan startup ordering regresses, the token could leak. Mitigation: an explicit init-order assertion at the top of app.py lifespan and a regression test that mints a startup error before configure_logging and asserts no token in caplog.
- Full-rebuild upsert-and-sweep approach means a crash mid-rebuild leaves a partially-marked-deleted state. Mitigation: sweep_orphans runs only after all fetch passes succeed; index_runs row records partial status; next rebuild fully overwrites. Documented in README.
- macOS GUI-launched Joplin inherits a near-empty PATH. Launcher recipe in README must use absolute paths for `uv` (e.g. /opt/homebrew/bin/uv). Risk of ENOENT on first run; mitigated by README guidance and doctor warning when PATH lookup of uv fails.
- respx autouse 'GET-only' guard in tests catches indirect httpx use, but only if the test suite covers the relevant code path. Mitigation: AST grep test that `import httpx` only appears in joplin_client.py acts as a static second gate.
- Per-request reader sqlite connections under high read concurrency could exhaust file descriptors. Acceptable at v0.1 scale (one user, localhost); flag for v0.2 if profiling shows pressure.
- Ephemeral token file on Windows is not protected by ACLs (chmod is a no-op). Documented limitation; matches PRD's 'local-first' threat model but flagged for production deployers.

## Scope adjustments vs. PRD (from completeness audit)

The critic flagged these PRD items as ambiguous or uncovered; resolutions are folded into the decisions above:
- **§3.1** (owner: indexer): In-scope item 'basic placeholder fields for attachment/resource indexing' — §3.2 excludes deep resource indexing 'beyond basic placeholder fields', implying some minimal resource field handling is in scope. No plan addresses resources/attachments at all (schema has no resources table; joplin_client has no get_resources; indexer plan does not enumerate resources).
- **§4.6** (owner: repositories): Workflow view 'notes likely requiring review' — listed as a v0.1 user story view. None of repositories.fetch_* methods or api views cover this; only recent/inbox/untagged/todos/stale are planned. Either drop from PRD or define a heuristic.
- **§7.2** (owner: config): Config schema must accept [indexing] table with stale_days and inbox_folder_names. Config plan defines IndexingConfig but does not show TOML-to-IndexingConfig mapping explicitly; verify TOML loader maps [indexing] section.
- **§11.2** (owner: indexer): Incremental sync must handle folder-change events ('if folder changed, fetch folders or target folder') and tag-change events explicitly. Indexer plan describes note events well but tag/folder event dispatch is underspecified — decision notes that cross-note tag-membership changes are deferred to full rebuild, which may leave stale data between rebuilds. Confirm acceptability.
- **§13.2** (owner: api): GET /api/note/{note_id} response shape — PRD says 'indexed note metadata and body'. api_models.NoteDetail is declared but not specified; ensure body is included (vs the body-stripped NoteSummary used elsewhere) and that this is the ONLY endpoint returning body.
- **§15.1** (owner: cli): CLI 'serve' lacks an explicit --api-token flag. PRD §15.1 lists --host/--port/--config/--db/--log-level; api_token comes only via env or config. Confirm intentional (matches PRD literally) — flag only if launcher_integration needs CLI injection.
- **§22** (owner: testing): Implementation sequence step 16 'Add README and manual test checklist'. README skeleton is in scaffold; manual checklist is in testing (tests/manual/CHECKLIST.md) and launcher_integration (scripts/launcher-smoke-test.md). Two manual checklists exist — consolidate or clarify scope of each.
- **§24** (owner: testing): Definition of Done item 'inbox view works' is explicitly listed but no acceptance criterion §18.x covers inbox specifically; testing plan maps 18.5/18.6 to recent and untagged but inbox is covered only inside test_api_views.py. Ensure inbox has its own assertion against configured inbox_folder_names.
- **§3.1** (owner: db): Resource/attachment 'basic placeholder fields' — schema has source_url on notes but no resources table or note_resources link. If 'placeholder' means just source_url, plans cover it implicitly; if it means a resources stub table, no plan provides one. Decision needed.
