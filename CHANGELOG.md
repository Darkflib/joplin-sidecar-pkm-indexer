# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/darkflib/joplin-sidecar-pkm-indexer/releases/tag/v0.1.0
