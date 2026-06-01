# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [0.2.2] — 2026-05-31

Dashboard polish only — still **read-only** (no mutation behaviour; every Joplin
call remains a GET). No schema change.

### Added
- **"Needs review" column** on the dashboard, wired to the existing
  `GET /api/notes/review` endpoint.
- **System colour-scheme support** — the dashboard now follows
  `prefers-color-scheme` with a full dark theme.
- Each note row gains an **"open in Joplin"** link
  (`joplin://x-callback-url/openNote?id=…`), a relative "updated" time (absolute
  time on hover), and a todo checkbox state; search results show a highlighted
  match snippet. Each column header shows its item count.

### Changed
- Nav bar now carries the index counts (notes/folders/tags/tasks), the search
  box, last-sync time, a **Sync** button, and the Joplin online/offline badge,
  freeing the body for content.
- The note lists are now a horizontally scrolling strip of fixed-width cards,
  each scrolling vertically on its own, so columns can be wider and carry more
  detail.

## [0.2.1] — 2026-05-31

### Added
- **First-run backfill** — on `serve` startup, if the index has never been fully
  built and a Joplin token is configured, a one-time full rebuild is kicked off
  automatically. This fixes the launcher (serve-only) path, where the background
  loop runs only incremental sync and the index would otherwise stay empty.
  Controlled by `[indexing].rebuild_on_empty_start` (default `true`).

### Fixed
- `__version__` (and `/health`/`/api/status` `version`) was still reporting
  `0.1.0`; it now tracks the package version.

## [0.2.0] — 2026-05-31

Builds on v0.1 (still read-only; every new Joplin call is a GET). **Schema v2 —
delete the index and resync to upgrade** (it's derived state).

### Added
- **Incremental tag-membership refresh** — `reindex_note` now refreshes a note's
  tags via the new GET-only `get_note_tags`, so tag changes no longer wait for a
  full rebuild (closes a v0.1 limitation).
- **Resource (attachment) metadata indexing** — `resources` + `note_resources`
  tables; `note_resources` is derived from `extracted_links ∩ resources`. Exposed
  via `resource_count` in `/api/status` and `GET /api/note/{id}/resources`. (Still
  no blob download/OCR.)
- **Backlinks & graph** — `GET /api/note/{id}/backlinks` and `GET /api/graph`
  (note→note internal-link edges + nodes, with a `truncated` flag).
- **"Needs review" view** — `GET /api/notes/review`: stale notes that are either
  untagged or carry unchecked tasks.

### Changed
- Schema bumped to v2 (resources/note_resources, `extracted_links.target` index).

### Notes
- Dashboard graph visualisation is deferred (strict CSP forbids CDN JS); the graph
  is available via the API.

## [0.1.0] — 2026-05-31

First release: a local-first, **read-only** Joplin PKM sidecar. Definition-of-Done
verified end-to-end against a live Joplin (~2200 notes indexed); see
[tests/manual/DOD-RESULTS.md](tests/manual/DOD-RESULTS.md).

### Added
- GET-only async Joplin Data API client (mutation is structurally impossible;
  enforced by an AST test) routed through a transport that refuses any non-Joplin
  URL.
- Rebuildable SQLite index with FTS5 full-text search; full rebuild and
  event-driven incremental sync with a background poll loop.
- FastAPI service bound to localhost: public `/health`, bearer-authenticated
  `/api/*` (status, recent/inbox/untagged/todos/stale views, search, note detail
  + tasks/links, and index rebuild/sync/reindex commands), and a static dashboard
  at `/` with URL-fragment token handoff.
- Typer CLI: `serve`, `doctor`, `status` (`--json`), `index rebuild`/`sync`,
  `db path`, `open`.
- Security: localhost-only bind with `--allow-non-localhost` opt-in, ephemeral API
  token minted to a `0600` file, constant-time bearer auth, and a logging
  redaction filter that scrubs registered secrets from every record.
- Configuration with CLI > env > TOML > defaults precedence; `doctor` health
  checks; structured `event=…` logging.
- README, `docs/launcher.md` (joplin-sidecar-launcher integration), test
  acceptance map, and a manual checklist.

### Known limitations (v0.1)
- Attachments/resources are not indexed beyond the existing `source_url` field.
- The "notes likely requiring review" view is deferred to v0.2.
- Tag membership is refreshed only by a full rebuild, not by incremental note
  edits.
- The `inbox` view matches `[indexing].inbox_folder_names`; set it to your vault's
  actual inbox folder name(s).
- Windows under the launcher is experimental (the `0600` token-file permission is
  a no-op there).

[0.2.2]: https://github.com/darkflib/joplin-sidecar-pkm-indexer/releases/tag/v0.2.2
[0.2.1]: https://github.com/darkflib/joplin-sidecar-pkm-indexer/releases/tag/v0.2.1
[0.2.0]: https://github.com/darkflib/joplin-sidecar-pkm-indexer/releases/tag/v0.2.0
[0.1.0]: https://github.com/darkflib/joplin-sidecar-pkm-indexer/releases/tag/v0.1.0
