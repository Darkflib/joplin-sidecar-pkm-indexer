Here’s a Codex-ready PRD for the Python sidecar indexer. I’ve scoped it as v0.1 read-only, because mutation, AI, embeddings, and plugin-driven note actions should come later once the indexer is trustworthy.

---

PRD: Joplin PKM Sidecar v0.1 — Read-only Python Indexer and Local Dashboard

1. Objective

Build a local-first Python sidecar service that connects to the Joplin Data API, maintains a rebuildable SQLite index of notes and metadata, exposes useful read-only PKM views, and serves a minimal local dashboard/API.

The sidecar is intended to be launched by the existing Joplin Sidecar Launcher plugin, but it must also be runnable standalone from the command line.

The v0.1 goal is:

When Joplin is open, the sidecar can index the local Joplin profile via the Joplin Data API and provide a useful read-only dashboard showing recent notes, inbox notes, TODOs, stale notes, untagged notes, and full-text search.

2. Background

The user currently uses Joplin as their primary PKM system. Joplin remains the canonical source of truth for notes, sync, attachments, mobile capture, and editing.

The sidecar exists to provide workflow and intelligence features that Joplin does not provide natively, without replacing Joplin or mutating note data in the first version.

Long-term architecture:

Joplin Desktop
  └─ Joplin Sidecar Launcher Plugin
       └─ starts/stops Python sidecar
Python Sidecar
  ├─ reads from Joplin Data API
  ├─ polls Joplin event endpoint
  ├─ maintains rebuildable SQLite index
  ├─ exposes FastAPI endpoints
  ├─ serves local dashboard
  └─ later supports richer PKM workflows

3. Scope

3.1 In scope for v0.1

* Python sidecar service.
* FastAPI HTTP server.
* SQLite database.
* FTS5 full-text search.
* Joplin Data API client.
* Read-only indexing of:
    * notes;
    * folders/notebooks;
    * tags;
    * note-tag relationships.
* Incremental update via Joplin event endpoint.
* Full rebuild command.
* Minimal dashboard.
* JSON API.
* CLI entry point.
* Local-only security model.
* Token-based access to sidecar API.
* Structured logging.
* Basic tests.

3.2 Out of scope for v0.1

Do not implement:

* note mutation;
* editing notes;
* creating notes;
* deleting notes;
* moving notes;
* modifying tags;
* Joplin sync control;
* attachment/resource indexing beyond basic placeholder fields;
* OCR;
* embeddings;
* LLM calls;
* MCP server;
* multi-user support;
* remote access;
* cloud hosting;
* custom sync;
* plugin panel integration beyond being launchable;
* graph visualisation;
* automatic conflict resolution;
* block-level stable IDs;
* complex Markdown AST storage;
* database migrations beyond a simple initial schema mechanism.

4. User stories

4.1 Start sidecar

As a user, I can start the sidecar manually or via the Joplin launcher plugin.

Example:

pkm-sidecar serve --host 127.0.0.1 --port 8765

The service starts a local HTTP server bound to localhost.

4.2 Configure Joplin connection

As a user, I can provide:

* Joplin base URL;
* Joplin API token;
* sidecar database path;
* sidecar API token;
* polling interval.

Configuration should be possible through:

* environment variables;
* optional config file;
* CLI flags for key runtime values.

4.3 Initial index

As a user, I can trigger a full index/rebuild.

The sidecar fetches all notes, folders, tags, and note-tag relationships from Joplin and stores them in SQLite.

4.4 Incremental index

As a user, after the initial index, the sidecar polls Joplin’s event endpoint and updates changed entities.

4.5 Search notes

As a user, I can search indexed notes through the dashboard and JSON API.

4.6 View workflow queues

As a user, I can see useful read-only views:

* recently changed notes;
* inbox notes;
* untagged notes;
* notes containing TODO-style checkboxes;
* Joplin TODO notes;
* stale notes;
* notes likely requiring review.

4.7 Health and status

As a user or launcher plugin, I can call /health and /status to determine whether the sidecar is running and whether it can reach Joplin.

5. Design principles

1. Joplin remains canonical.
    * SQLite is derived state only.
    * The index can be deleted and rebuilt.
2. Read-only first.
    * v0.1 must not mutate Joplin data.
3. Local-first.
    * Bind to 127.0.0.1 by default.
    * Do not expose remote access.
4. Fail safely.
    * If Joplin is unavailable, the dashboard should still load with stale index data and show degraded status.
5. No hidden external calls.
    * No LLM calls.
    * No telemetry.
    * No cloud APIs.
6. Make state inspectable.
    * Expose status, last sync time, event cursor, counts, and recent errors.
7. Prefer boring correctness over cleverness.
    * SQLite, FastAPI, Pydantic, and explicit logs.

6. Technical stack

Required:

* Python 3.12+.
* FastAPI.
* Uvicorn.
* SQLite.
* Pydantic v2.
* httpx.
* Click or Typer.
* pytest.
* Ruff.
* mypy, strict mode — enforced in CI (the tree is clean, so keep it that way).
* uv for dependency management.

Suggested package structure:

pkm-sidecar/
  pyproject.toml
  README.md
  src/
    pkm_sidecar/
      __init__.py
      __main__.py
      cli.py
      config.py
      logging_config.py
      app.py
      security.py
      joplin_client.py
      indexer.py
      markdown_extract.py
      db.py
      schema.sql
      repositories.py
      services.py
      views.py
      dashboard.py
      models.py
      errors.py
  tests/
    test_config.py
    test_security.py
    test_markdown_extract.py
    test_repositories.py
    test_indexer.py

7. Configuration

7.1 Environment variables

Support these environment variables:

PKM_SIDECAR_HOST=127.0.0.1
PKM_SIDECAR_PORT=8765
PKM_SIDECAR_API_TOKEN=<random-local-token>
PKM_SIDECAR_DB_PATH=~/.local/share/pkm-sidecar/index.sqlite3
PKM_SIDECAR_CONFIG_PATH=~/.config/pkm-sidecar/config.toml
PKM_SIDECAR_LOG_LEVEL=INFO
JOPLIN_BASE_URL=http://127.0.0.1:41184
JOPLIN_TOKEN=<joplin-api-token>
JOPLIN_EVENT_POLL_SECONDS=10
JOPLIN_PAGE_LIMIT=100

7.2 Config file

Support optional TOML config:

[server]
host = "127.0.0.1"
port = 8765
api_token = "change-me"
[joplin]
base_url = "http://127.0.0.1:41184"
token = "..."
event_poll_seconds = 10
page_limit = 100
[database]
path = "~/.local/share/pkm-sidecar/index.sqlite3"
[logging]
level = "INFO"
[indexing]
stale_days = 90
inbox_folder_names = ["Inbox", "00 Inbox", "_Inbox"]

Environment variables should override config file values.

CLI flags should override both.

7.3 Config validation

On startup:

* reject non-localhost bind addresses unless --allow-non-localhost is explicitly provided;
* warn if no sidecar API token is configured;
* error if Joplin token is missing for indexing operations;
* expand ~ paths;
* create config/data directories as needed.

For v0.1, default should be localhost-only.

8. Security requirements

8.1 Bind address

Default bind address must be:

127.0.0.1

Do not bind to 0.0.0.0 unless explicitly requested.

8.2 Sidecar API authentication

All endpoints except the following require a bearer token:

GET /health
GET /
GET /static/*

Protected endpoints require:

Authorization: Bearer <PKM_SIDECAR_API_TOKEN>

If no sidecar API token is configured, protected endpoints should be disabled or return a clear startup warning. Preferred behaviour: generate an ephemeral token at startup and log where to find it, but do not print it repeatedly.

8.3 Joplin token handling

* Never log the Joplin token.
* Never expose the Joplin token through /status.
* Redact token-like values in diagnostics.
* Do not store the Joplin token in SQLite.
* If config file stores token, document that file permissions should be restrictive.

8.4 No mutation

v0.1 must not call Joplin write endpoints.

Allowed Joplin operations are read-only:

* fetch notes;
* fetch folders;
* fetch tags;
* fetch note-tag relationships;
* fetch events;
* ping/status checks.

8.5 No external network access

The sidecar should not call external services in v0.1.

All HTTP client calls should be to the configured Joplin base URL.

9. Database schema

Use SQLite.

Enable:

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

Initial schema:

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    title TEXT NOT NULL,
    updated_time INTEGER,
    created_time INTEGER,
    indexed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_time INTEGER,
    updated_time INTEGER,
    user_created_time INTEGER,
    user_updated_time INTEGER,
    is_todo INTEGER NOT NULL DEFAULT 0,
    todo_due INTEGER,
    todo_completed INTEGER,
    source_url TEXT,
    indexed_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(parent_id) REFERENCES folders(id)
);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_time INTEGER,
    updated_time INTEGER,
    indexed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS note_tags (
    note_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    indexed_at INTEGER NOT NULL,
    PRIMARY KEY (note_id, tag_id),
    FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS extracted_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    checked INTEGER NOT NULL,
    text TEXT NOT NULL,
    raw_line TEXT NOT NULL,
    indexed_at INTEGER NOT NULL,
    FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS extracted_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL,
    line_number INTEGER,
    link_text TEXT,
    target TEXT NOT NULL,
    link_type TEXT NOT NULL,
    indexed_at INTEGER NOT NULL,
    FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    notes_seen INTEGER NOT NULL DEFAULT 0,
    notes_updated INTEGER NOT NULL DEFAULT 0,
    folders_seen INTEGER NOT NULL DEFAULT 0,
    tags_seen INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title,
    body,
    content='notes',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body)
    VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO notes_fts(rowid, title, body)
    VALUES (new.rowid, new.title, new.body);
END;

Store event cursor in meta:

joplin.last_event_id
joplin.last_full_index_at
joplin.last_incremental_index_at

10. Joplin API client

Create JoplinClient.

Responsibilities:

* configure base URL and token;
* make authenticated GET requests;
* handle pagination;
* handle timeouts;
* raise typed errors;
* retry transient connection failures conservatively;
* redact token from logs.

Methods:

class JoplinClient:
    async def ping(self) -> bool: ...
    async def get_notes(self, *, fields: list[str], limit: int) -> AsyncIterator[dict]: ...
    async def get_note(self, note_id: str, *, fields: list[str]) -> dict: ...
    async def get_folders(self, *, fields: list[str], limit: int) -> AsyncIterator[dict]: ...
    async def get_tags(self, *, fields: list[str], limit: int) -> AsyncIterator[dict]: ...
    async def get_tag_notes(self, tag_id: str, *, fields: list[str], limit: int) -> AsyncIterator[dict]: ...
    async def get_events(self, *, cursor: str | None = None, limit: int = 100) -> dict: ...

Implementation notes:

* Use httpx.AsyncClient.
* Default timeout: 10 seconds.
* Events polling timeout may be shorter.
* Do not crash the service if Joplin is temporarily unavailable.
* Surface degraded status via /status.

11. Indexing behaviour

11.1 Full rebuild

Command:

pkm-sidecar index rebuild

or endpoint:

POST /api/index/rebuild

Full rebuild should:

1. Create an index_runs record.
2. Fetch all folders.
3. Fetch all notes.
4. Fetch all tags.
5. Fetch note-tag relationships.
6. Extract Markdown tasks and links.
7. Update FTS.
8. Store completion status.
9. Store joplin.last_full_index_at.

For v0.1, full rebuild may clear and recreate derived tables.

11.2 Incremental sync

The service should run a background task while serving.

Every JOPLIN_EVENT_POLL_SECONDS:

1. Fetch events since last cursor.
2. For each event:
    * if note changed, fetch note and upsert it;
    * if folder changed, fetch folders or target folder;
    * if tag changed, refresh tags;
    * if deletion detected, mark entity as deleted if possible.
3. Update cursor only after successful processing.
4. Store last incremental sync timestamp.

If the event endpoint behaviour is insufficient or ambiguous, fall back to conservative partial refresh for affected object types.

11.3 Re-index single note

For v0.1, provide internal function:

async def reindex_note(note_id: str) -> None:
    ...

Expose endpoint:

POST /api/index/note/{note_id}

Protected by sidecar API token.

11.4 Rebuild derived data

When a note is upserted:

* update notes;
* delete/recreate extracted tasks for that note;
* delete/recreate extracted links for that note;
* allow FTS triggers to update index.

12. Markdown extraction

Implement simple line-oriented extraction, not a full AST.

12.1 Tasks

Recognise Markdown task lines:

- [ ] Do thing
- [x] Done thing
* [ ] Also valid
+ [ ] Also valid

Extract:

* note ID;
* line number;
* checked boolean;
* task text;
* raw line.

12.2 Links

Recognise basic Markdown links:

[link text](target)

Recognise Joplin internal links if present:

[title](:/note-id)

Classify:

internal_joplin
external_url
relative
other

Do not attempt complex nested Markdown parsing in v0.1.

13. API endpoints

13.1 Public endpoints

GET /health

Returns:

{
  "ok": true,
  "service": "pkm-sidecar",
  "version": "0.1.0"
}
GET /

Returns minimal dashboard HTML.

13.2 Protected API endpoints

All /api/* endpoints require bearer token.

GET /api/status

Returns:

{
  "service": "pkm-sidecar",
  "version": "0.1.0",
  "joplin": {
    "configured": true,
    "reachable": true,
    "base_url": "http://127.0.0.1:41184",
    "last_error": null
  },
  "database": {
    "path": "...",
    "note_count": 1234,
    "folder_count": 42,
    "tag_count": 50,
    "task_count": 200
  },
  "indexing": {
    "last_full_index_at": 1710000000,
    "last_incremental_index_at": 1710000300,
    "last_event_id": "..."
  }
}

Do not include Joplin token.

POST /api/index/rebuild

Triggers full rebuild.

For v0.1, this may run synchronously if small, but preferred behaviour is background task with status tracking.

POST /api/index/sync

Triggers immediate incremental sync.

POST /api/index/note/{note_id}

Re-indexes one note.

GET /api/notes/recent?limit=50

Returns recent notes by updated_time.

GET /api/notes/inbox

Returns notes in configured inbox folders.

GET /api/notes/untagged

Returns notes with no tags.

GET /api/notes/todos

Returns notes with Joplin TODO state or extracted Markdown tasks.

GET /api/notes/stale?days=90

Returns notes older than threshold.

GET /api/search?q=...&limit=50

FTS search over title and body.

GET /api/note/{note_id}

Returns indexed note metadata and body.

Optional for v0.1:

GET /api/note/{note_id}/tasks
GET /api/note/{note_id}/links

14. Dashboard

Serve a minimal HTML dashboard at /.

Dashboard can be server-rendered Jinja2, simple static HTML with fetch calls, or FastAPI templates.

Minimum pages/sections:

* status;
* recently changed notes;
* inbox notes;
* untagged notes;
* TODO notes/tasks;
* stale notes;
* search box.

The dashboard does not need authentication for the top-level page, but API calls should require token. For local use, one acceptable pattern is to include the sidecar token only if the dashboard was opened with a local session token generated at startup. Simpler v0.1 option: dashboard shows non-sensitive status and instructs user to configure token for API-backed views.

Preferred v0.1 pragmatic option:

* protect dashboard with the same bearer token via a query parameter only for local use, or
* disable dashboard API calls when no token is available.

Security note: avoid putting the Joplin token in browser-accessible JavaScript.

15. CLI

Use Click or Typer.

Commands:

pkm-sidecar serve
pkm-sidecar index rebuild
pkm-sidecar index sync
pkm-sidecar status
pkm-sidecar doctor
pkm-sidecar db path

15.1 serve

Starts FastAPI server.

Options:

--host 127.0.0.1
--port 8765
--config ~/.config/pkm-sidecar/config.toml
--db ~/.local/share/pkm-sidecar/index.sqlite3
--log-level INFO

15.2 index rebuild

Runs full rebuild, then exits.

15.3 index sync

Runs one incremental sync pass, then exits.

15.4 status

Prints database and Joplin status.

15.5 doctor

Checks:

* config file readable;
* database directory writable;
* Joplin base URL reachable;
* Joplin token valid;
* SQLite FTS5 available;
* sidecar bind address is local;
* no obvious security footguns.

16. Logging

Use Python logging.

Default log level:

INFO

Log format:

timestamp level logger message

Required log events:

* service start;
* config loaded;
* database opened;
* full index started/completed/failed;
* incremental sync started/completed/failed;
* Joplin unreachable;
* API authentication failures, without logging tokens;
* unexpected exceptions.

Do not log note bodies by default.

Debug mode may log note IDs and titles, but still not bodies unless explicitly enabled.

17. Error handling

17.1 Joplin unavailable

If Joplin is unavailable:

* /health should still return service health;
* /api/status should show Joplin as unreachable;
* dashboard should display degraded status;
* background sync should retry later;
* existing index remains usable.

17.2 Token invalid

If Joplin token is invalid:

* show clear status error;
* do not repeatedly spam logs;
* do not delete existing index.

17.3 Database locked

Handle SQLite lock errors with:

* short retry/backoff;
* clear error logging;
* no data corruption.

17.4 Event cursor invalid

If event cursor is invalid or Joplin event history cannot be followed:

* log warning;
* run conservative full refresh or require rebuild;
* do not silently mark index current.

18. Acceptance criteria

18.1 Service starts

Given valid config, when running:

pkm-sidecar serve --host 127.0.0.1 --port 8765

Then:

* service starts;
* /health returns OK;
* logs show startup.

18.2 Doctor command works

Given Joplin is running and token is valid, when running:

pkm-sidecar doctor

Then output shows:

* config valid;
* database writable;
* Joplin reachable;
* token accepted;
* FTS5 available.

18.3 Full rebuild works

Given Joplin contains notes, when running:

pkm-sidecar index rebuild

Then:

* SQLite database is created;
* notes table contains indexed notes;
* folders table contains folders;
* tags table contains tags;
* note_tags table contains relationships;
* notes_fts contains searchable content;
* index_runs records success.

18.4 Search works

Given a note contains the word RabbitMQ, when calling:

GET /api/search?q=RabbitMQ

Then:

* matching note is returned;
* title, note ID, updated time, and snippet/preview are included.

18.5 Recent notes view works

Given notes exist, when calling:

GET /api/notes/recent

Then:

* notes are returned in descending updated order.

18.6 Untagged view works

Given at least one note has no tags, when calling:

GET /api/notes/untagged

Then:

* untagged notes are returned.

18.7 Markdown task extraction works

Given a note contains:

- [ ] Write PRD
- [x] Confirm plugin spike

Then extracted_tasks contains two rows with correct checked state.

18.8 Incremental sync works

Given initial index exists, when a note is modified in Joplin and incremental sync runs, then:

* changed note is updated in SQLite;
* body_hash changes;
* FTS returns new content;
* last event cursor is advanced.

18.9 Joplin outage does not break dashboard

Given Joplin is stopped but index exists, when opening dashboard:

* dashboard loads;
* stale indexed data is available;
* Joplin status shows unreachable.

18.10 No mutation

During v0.1 tests, the sidecar must not issue write/delete requests to Joplin.

19. Testing requirements

19.1 Unit tests

Required tests:

* config loading and precedence;
* localhost bind validation;
* token redaction;
* Markdown task extraction;
* Markdown link extraction;
* FTS query escaping/safety;
* repository upsert functions;
* ring/error handling where applicable;
* Joplin client pagination using mocked HTTP responses;
* event processing using mocked HTTP responses.

19.2 Integration tests

Use temporary SQLite database.

Mock Joplin API using respx, pytest-httpx, or a small local FastAPI test server.

Test:

* full rebuild;
* incremental note update;
* tag relationship indexing;
* search endpoint;
* status endpoint.

19.3 Manual tests

Manual tests against real Joplin:

* run doctor;
* run full rebuild;
* modify note in Joplin;
* trigger sync;
* search for modified content;
* stop Joplin;
* confirm degraded dashboard.

20. Performance expectations

v0.1 should comfortably handle:

* 10,000 notes;
* average body size under 50 KB;
* 1,000 tags;
* full rebuild in minutes, not seconds;
* incremental update in seconds.

Do not over-optimise.

Use pagination and batch database transactions.

21. Privacy requirements

* No telemetry.
* No analytics.
* No external calls.
* No note bodies in logs.
* No Joplin token in API responses.
* No automatic upload/export.
* Database stays local.

22. Suggested implementation sequence

1. Create project scaffold with uv.
2. Add config loading.
3. Add logging.
4. Add SQLite schema initialisation.
5. Add Joplin client with mocked tests.
6. Add repository layer.
7. Add full rebuild indexing.
8. Add Markdown task/link extraction.
9. Add FTS search.
10. Add FastAPI /health and /api/status.
11. Add protected API auth.
12. Add workflow endpoints.
13. Add incremental event polling.
14. Add dashboard.
15. Add CLI commands.
16. Add README and manual test checklist.

23. README requirements

README must include:

* purpose;
* architecture summary;
* installation using uv;
* configuration;
* example Joplin launcher plugin command;
* how to get Joplin API token;
* how to run doctor;
* how to rebuild index;
* how to start server;
* security model;
* limitations;
* v0.1 non-goals.

Example launcher command:

uv run pkm-sidecar serve --host 127.0.0.1 --port 8765

or if installed as tool:

uv tool run pkm-sidecar serve --host 127.0.0.1 --port 8765

24. Definition of done

v0.1 is done when:

* package installs/runs locally;
* pkm-sidecar doctor works;
* pkm-sidecar index rebuild indexes real Joplin data;
* pkm-sidecar serve starts a local API;
* /health works without auth;
* /api/status works with auth;
* /api/search returns FTS results;
* recent, inbox, untagged, TODO, and stale views work;
* incremental sync updates changed notes;
* service tolerates Joplin being temporarily unavailable;
* tests pass;
* README documents setup, risks, and limitations;
* no Joplin mutation endpoints exist.

25. Explicit instruction to Codex

Implement the smallest robust read-only indexer that proves the sidecar architecture.

Do not add AI, embeddings, MCP, note mutation, sync replacement, graph visualisation, or multi-user features. Keep the design boring, local, secure, and rebuildable.

---

A couple of implementation nudges I’d give Codex alongside this:

Use read-only as a hard invariant in v0.1. I’d even make the Joplin client only implement GET methods at first, so mutation is structurally impossible rather than merely “not currently used”.

Also, make /api/status genuinely useful early. That endpoint becomes the contract between the launcher plugin, dashboard, and future Joplin panel.