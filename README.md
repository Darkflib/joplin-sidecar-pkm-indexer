# pkm-sidecar

> Local-first, **read-only** Joplin PKM sidecar — indexes your notes into a rebuildable SQLite index and serves a minimal local dashboard and JSON API.

> [!NOTE]
> This is the v0.1 scaffold. Most sections below are placeholders that are filled
> in as the build progresses (see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)).

## Purpose

pkm-sidecar provides workflow and intelligence views over your Joplin notes
(recent, inbox, untagged, TODOs, stale, full-text search) **without** replacing
Joplin or mutating note data. Joplin remains the canonical source of truth; the
SQLite index is derived state that can be deleted and rebuilt at any time.

## Architecture summary

```
Joplin Desktop
  └─ Joplin Sidecar Launcher Plugin
       └─ starts/stops the Python sidecar
Python Sidecar (this project)
  ├─ reads from the Joplin Data API (GET-only)
  ├─ polls the Joplin event endpoint
  ├─ maintains a rebuildable SQLite index (FTS5 search)
  ├─ exposes FastAPI endpoints
  └─ serves a local dashboard
```

## Installation (uv)

```sh
uv sync --extra dev      # create the environment with dev tooling
uv run pkm-sidecar --version
```

## Configuration

_Placeholder — environment variables, optional TOML config, and CLI flags
(precedence: CLI > env > TOML > defaults). See PRD §7._

## Example Joplin launcher plugin command

```sh
uv run pkm-sidecar serve --host 127.0.0.1 --port 8765
```

or, if installed as a tool:

```sh
uv tool run pkm-sidecar serve --host 127.0.0.1 --port 8765
```

## How to get a Joplin API token

_Placeholder — Joplin → Tools → Options → Web Clipper → Authorisation token._

## Running doctor

```sh
uv run pkm-sidecar doctor
```

_Placeholder — checks config, database, Joplin reachability, token validity,
FTS5 availability, and bind-address safety._

## Rebuilding the index

```sh
uv run pkm-sidecar index rebuild
```

## Starting the server

```sh
uv run pkm-sidecar serve --host 127.0.0.1 --port 8765
```

## Security model

_Placeholder — localhost-only bind by default; bearer-token auth on `/api/*`;
the Joplin token is never logged, never returned by the API, and never stored in
SQLite. No external network calls beyond the configured Joplin base URL. See PRD §8._

## Limitations

_Placeholder. Notable v0.1 decisions:_

- Attachments/resources are not indexed beyond the existing `source_url` field.
- The "notes likely requiring review" view is deferred to v0.2 (no defined heuristic yet).
- Cross-note tag-membership changes may be stale between full rebuilds.

## v0.1 non-goals

No note mutation, editing, creation, or deletion; no sync control; no OCR,
embeddings, or LLM calls; no MCP server; no multi-user or remote access. See PRD §3.2.

## Development

```sh
uv run ruff check .
uv run ruff format .
uv run pytest
uv run mypy src      # optional
```
