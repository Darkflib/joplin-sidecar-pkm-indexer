# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **An interrupted full rebuild no longer leaves a silently partial index.**
  `reset_derived` wipes tasks/links/tags up front, but only the final transaction
  stamps `last_full_index_at` — so a rebuild killed or failing in between left
  that stamp holding the *previous* run's value, and nothing noticed. The views
  went on answering with partial data (an "Untagged" column listing every note,
  an empty "TODOs") until someone rebuilt by hand; only the very first rebuild
  self-healed. A `joplin.rebuild_in_progress` flag is now raised in the same
  transaction as the wipe and lowered in the same one that stamps completion, so
  its presence means exactly "the derived tables are empty and not yet refilled".
  `serve` checks it at startup and rebuilds. Recovery is deliberately not gated
  on `rebuild_on_empty_start`: that option skips the cost of backfilling a fresh
  index, it is not an opt-in to serving a known-broken one.
- The same flag covers a rebuild that *fails* partway (Joplin going away
  mid-run), which left the index equally partial and equally unmarked.

### Added
- `/api/status` reports `indexing.index_incomplete` and
  `indexing.rebuild_in_progress`. Both true means a rebuild is running now;
  incomplete without in-progress means one was interrupted and the views are
  serving partial data.
- `doctor` gained an `index_complete` check that fails, with the rebuild command
  to run, when the derived tables were left half-wiped.

No schema change. Fixes three defects that only show up over a long unattended
run — the sidecar's actual deployment mode — plus the dependency/CI maintenance
below.

### Fixed
- **`index_runs` no longer grows without bound.** Every incremental tick opened a
  row whether or not anything had changed, so an idle sidecar polling every 10s
  wrote ~8,640 rows a day (~3.1M a year) to a table nothing reads back and
  nothing pruned. A tick that finds no events now refreshes the "last synced"
  stamp and returns without opening a row or emitting start/complete events; runs
  that do work — and failures, which are the rows worth keeping — are still
  recorded, and `index_runs` is trimmed to the most recent 500.
- **A Joplin outage no longer floods the log.** The background loop logged a full
  traceback *and* an `index.incremental.failed` ERROR line on every tick, so an
  overnight outage produced thousands of each. Both are now rate-limited to one
  per error type per five minutes; the first failure is logged in full and, if it
  persists, the next line reports how many ticks have failed in a row. Only a
  tick that runs end to end counts as recovery and clears the cooldown — `/events`
  merely answering is not enough, because when it is the *processing* of a batch
  that keeps failing the cursor never advances, so the next tick refetches the
  same batch and would otherwise log afresh every poll. `/api/status` keeps
  reporting `last_error` throughout, and a one-shot `index sync` still logs its
  single failure. This is what `docs/launcher.md` already claimed about log
  hygiene under the launcher's "Copy diag".
- **The non-localhost bind guard is actually wired up.** `serve --host 0.0.0.0
  --allow-non-localhost` with no `PKM_SIDECAR_API_TOKEN` started happily behind
  an auto-generated token the operator had never seen: the check existed and was
  unit-tested, but nothing in `app.py` or `cli.py` ever called it — so the
  README's security-model promise was not enforced. Now checked in both `serve`
  (exit 3) and the app lifespan, and before `resolve_api_token` runs, so a
  refused bind never writes a token file it is about to abandon. The lifespan
  also re-runs `validate_bind_address`, which previously lived only in the CLI —
  an embedder calling `create_app` directly could bind a non-loopback host
  without ever setting `allow_non_localhost`.

### Changed
- `assert_non_local_bind_has_token` now takes the `AppConfig` alone rather than
  an `AppConfig` and a `ResolvedToken`, so it can run before a token is minted.
  The two forms are equivalent — an ephemeral token is minted exactly when none
  is configured.
- Exit code 3 now covers both unsafe non-localhost binds (no override, or no
  configured API token); README and the CLI epilogue say so.
- **Dependencies refreshed** (`uv lock --upgrade`): FastAPI 0.136→0.141,
  Starlette 1.2→1.6, uvicorn 0.48→0.52, Typer 0.26→0.27,
  pydantic-settings 2.14→2.15, plus dev tooling (ruff 0.15→0.16,
  mypy 2.1→2.3, pytest 9.0→9.1, coverage, pre-commit). Declared version
  floors in `pyproject.toml` now track the versions CI actually exercises.
- **Pre-commit hooks bumped** — `ruff-pre-commit` v0.6.9→v0.16.2 (it was ten
  minor versions behind the `ruff` used in CI, so local hooks and CI could
  disagree about formatting) and `pre-commit-hooks` v5.0.0→v6.0.0.
- **Mypy is now a blocking CI check** rather than `continue-on-error`. The tree
  is clean under `--strict`, so there was nothing to grandfather in.

### Added
- CI: `uv lock --check`, so a dependency bump can't merge without the matching
  lockfile; a `build` job that builds the sdist/wheel and asserts `schema.sql`,
  `templates/` and `static/` are actually packaged in **both**; an explicit
  read-only `permissions` block; and `persist-credentials: false` on every
  checkout, since no job runs authenticated git commands.
- `.github/dependabot.yml` — monthly GitHub Actions and uv dependency updates.
- 21 regression tests covering the three fixes above (313 total, 91% coverage).
  The suite previously only ever ran short scenarios, which is why none of these
  defects showed up — the new tests assert what happens on the *n*th tick, during
  a sustained outage, and on a refused start.
- An autouse fixture in `tests/conftest.py` resetting the process-global failure
  log guard around every test. It deliberately spans ticks, so it also spanned
  tests: without the reset, the first test to log a given error type silenced
  every later one and log-volume assertions passed or failed on test ordering.

### Removed
- A stray `.DS_Store` committed at the repo root (now gitignored).

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

### Fixed
- Last-sync time showed "57y ago" — the index meta timestamps are epoch
  *seconds* but the relative-time formatter expected milliseconds; the value is
  now scaled and reads "never synced" when the index has never been built.
- The token paste banner (and the search column) stayed visible even when not
  needed: an id/class `display:` rule overrode the `hidden` attribute. Added a
  `[hidden] { display: none !important }` reset.

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
