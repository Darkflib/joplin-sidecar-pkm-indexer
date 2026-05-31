# pkm-sidecar

> Local-first, **read-only** Joplin PKM sidecar — indexes your notes into a rebuildable SQLite index and serves a minimal local dashboard and JSON API.

pkm-sidecar gives you workflow views over your Joplin notes — recent, inbox,
untagged, TODOs, stale, and full-text search — **without** changing anything in
Joplin. Joplin stays the source of truth; the SQLite index is derived state you
can delete and rebuild at any time.

## Architecture

```
Joplin Desktop
  └─ Joplin Sidecar Launcher Plugin
       └─ starts/stops the Python sidecar
Python Sidecar (this project)
  ├─ reads the Joplin Data API (GET-only — mutation is structurally impossible)
  ├─ polls the Joplin event endpoint for incremental updates
  ├─ maintains a rebuildable SQLite index (FTS5 search)
  ├─ serves a localhost FastAPI: /health, /api/*, and a dashboard at /
  └─ binds to 127.0.0.1 only; the API requires a bearer token
```

## Installation (uv)

```sh
uv sync --extra dev          # create the environment
uv run pkm-sidecar --version
```

## Getting a Joplin API token

In Joplin: **Tools → Options → Web Clipper → enable the service**, then copy the
**Authorization token** shown there. The sidecar only ever uses it for read
(GET) requests.

## Configuration

Resolution order (highest wins): **CLI flags > environment > config file > defaults**.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PKM_SIDECAR_HOST` | `127.0.0.1` | bind host (localhost only unless `--allow-non-localhost`) |
| `PKM_SIDECAR_PORT` | `8765` | bind port |
| `PKM_SIDECAR_API_TOKEN` | _(ephemeral)_ | bearer token for `/api/*`; if unset, one is generated |
| `PKM_SIDECAR_DB_PATH` | `~/.local/share/pkm-sidecar/index.sqlite3` | SQLite index path |
| `PKM_SIDECAR_CONFIG_PATH` | `~/.config/pkm-sidecar/config.toml` | optional TOML config |
| `PKM_SIDECAR_LOG_LEVEL` | `INFO` | log level |
| `JOPLIN_BASE_URL` | `http://127.0.0.1:41184` | Joplin Data API base |
| `JOPLIN_TOKEN` | _(unset)_ | Joplin Web Clipper token (required for indexing) |
| `JOPLIN_EVENT_POLL_SECONDS` | `10` | incremental poll interval |
| `JOPLIN_PAGE_LIMIT` | `100` | Joplin pagination size |

Optional `config.toml` (keep it `chmod 600` if it holds a token):

```toml
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
```

### Where the API token comes from

`/api/*` requires `Authorization: Bearer <PKM_SIDECAR_API_TOKEN>`. If you don't
set one, the sidecar **mints an ephemeral token at startup**, writes it `0600` to
a per-user runtime file, and logs that file's *path* (never the value) once. The
dashboard receives the token via a URL fragment from `pkm-sidecar open` (or the
launcher) — never embedded in HTML.

## Running

```sh
uv run pkm-sidecar doctor        # check config, DB, Joplin reachability, FTS5, bind safety
uv run pkm-sidecar index rebuild # build the index from Joplin
uv run pkm-sidecar serve --host 127.0.0.1 --port 8765
uv run pkm-sidecar open          # open the dashboard with the token handed off
uv run pkm-sidecar status --json # the /api/status payload (token never printed)
uv run pkm-sidecar db path       # absolute SQLite path
```

Exit codes: `0` ok · `1` runtime failure · `2` config error · `3` non-localhost
bind without `--allow-non-localhost` · `4` indexing finished with errors ·
`5` event cursor invalid (run a rebuild) · `130` interrupted.

> **Tip:** the `inbox` view matches folders named in `[indexing].inbox_folder_names`
> (default `Inbox`, `00 Inbox`, `_Inbox`). If it comes back empty, set this to your
> vault's actual inbox folder name(s).

## Launcher integration

This sidecar is designed to be started by the
[Joplin Sidecar Launcher](../joplin-sidecar-launcher) plugin. See
[docs/launcher.md](docs/launcher.md) for the exact Command/Arguments/Health-URL
settings and how to supply tokens (the launcher has no env-var UI).

## Security model

- **Localhost only.** Binds `127.0.0.1` by default; a non-local bind needs
  `--allow-non-localhost` and refuses to start on an ephemeral token.
- **Bearer auth** on every `/api/*` route (constant-time compare). `/health`,
  `/`, and `/static/*` are public.
- **No mutation.** The Joplin client exposes only GET methods (enforced by an AST
  test) and all traffic is fenced to the configured Joplin base URL by a custom
  transport — no other network calls.
- **Token hygiene.** The Joplin token is never logged, never returned by the API,
  and never stored in SQLite. A redaction filter scrubs registered secrets from
  every log record; note bodies are never logged.

## Limitations

- Resource indexing is metadata only (id/title/mime/filename/size) — no blob
  download or OCR.
- Graph data is available via `GET /api/graph`; the dashboard has no graph
  visualisation yet (the strict CSP forbids loading a CDN graph library).
- Windows under the launcher is experimental; the `0600` token-file permission is
  a no-op there.
- Upgrading the index schema (e.g. v1 → v2) is delete-and-resync — the index is
  derived state, so there's no migration step.

## v0.1 non-goals

No note mutation/creation/deletion, no sync control, no OCR/embeddings/LLM calls,
no MCP server, no multi-user or remote access.

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -m 'not manual' --cov=pkm_sidecar --cov-report=term-missing --cov-fail-under=70
```

See [tests/README.md](tests/README.md) for the PRD §18 acceptance-criteria map and
[tests/manual/CHECKLIST.md](tests/manual/CHECKLIST.md) for the manual test pass.
