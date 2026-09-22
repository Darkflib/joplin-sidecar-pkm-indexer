# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Title worker and `pkm-sidecar enrich titles`** — completes step 4 of
  [docs/enrichment.md](docs/enrichment.md). Walks candidates, routes each to the
  URL or the model, and stores suggestions. Still writes nothing to Joplin, and
  still refuses to run unless `[enrichment] enabled` is set.
  - A plain serial queue. The measured workload is an overnight batch of ~87
    model calls at about a second each, then a handful of notes a day, so the
    absence of concurrency is the design rather than a gap in it.
  - Cached by suggestion identity: a repeat run over 12 real notes went from
    19s to 0.38s and spent no model calls.
  - A single note failing is counted, not fatal — one bad note must not abandon
    an overnight batch.
  - Runs usefully with **no model host at all**: the slug path needs none, which
    matters because for a dead bookmark the URL is the only title source that
    will ever exist.
- **Title generation (step 4 of [docs/enrichment.md](docs/enrichment.md))** —
  the slug path, prompt shaping and output cleaning. No worker yet, so nothing
  runs on its own.
  - **The offline slug path handles 20 of the vault's 35 URL-titled notes** with
    no model and no network, and it is the only path that works on a dead link —
    four of those domains no longer resolve at all. Routing across the whole
    vault: 87 to the model, 20 to the slug path, 37 skipped for having nothing
    to work from (down from 56 before the slug path existed).
  - Slugs are parsed from the URL in the **body**, not the title. Joplin cuts an
    auto-derived title at 80 characters, so the title's copy is usually a
    fragment — parsing it produced "…How to Maintain Intense Motiva" and "…Tri
    Audio and S" across the real vault.
  - URLs carrying a credential in the query are refused outright: the words are
    not there anyway, and it keeps a live key out of a suggestion row.
  - Opaque URLs decline rather than guess. Inventing a title from
    `share.google/crvtpycs…` is worse than leaving the note for a human.
  - `clean_title` enforces the format the prompt asks for, because the smaller
    models ignored "no quotes" in 3 of 5 measured cases: quote stripping, a
    "Title:" prefix, trailing punctuation, multi-line answers, and rejecting a
    proposal identical to the title the note already has.
- **Ollama client (step 3 of [docs/enrichment.md](docs/enrichment.md))** — a
  fenced async client for the enrichment model host, plus `doctor` checks for
  reachability, model presence and transport safety. Nothing calls it yet.
  - `GenerateResult` keeps `thinking` separate from `response` and flags the
    case where a reasoning model spends its whole budget before answering.
    Measured, not hypothetical: `gpt-oss:20b` at `num_predict=24` returns an
    empty string with a full thinking channel, and `think: false` does not
    suppress it. Collapsing the fields would have produced silent empty titles.
  - `embed()` rejects a response whose vector count differs from the input
    count — silently returning fewer would misalign every nearest-neighbour
    result with no error.
  - `generate()` defaults to `temperature: 0`, since a 1.5B model was measured
    giving a different, wrong answer for the same note between runs at 0.2.
  - `doctor` gained a **`WARN`** tier that does not affect the exit code, for
    deliberate trade-offs as distinct from breakage. Plain HTTP to a model host
    on a private network is the first user of it.

### Changed
- `LocalOnlyTransport` → `SingleOriginTransport`, and
  `assert_url_is_joplin_base` → `assert_url_matches_base`. The fence was always
  generic; it now has a second caller, so the name no longer claims otherwise.
  Each client owns its own instance, so permitting Ollama does not widen what
  the Joplin client can reach.
- **Title candidate detection (step 2 of [docs/enrichment.md](docs/enrichment.md))** —
  `enrichment.title_candidates`: pure SQL and regex, no model and no network.
  Flags empty titles, Joplin's own placeholders, bare dates, URLs, filenames, and
  titles Joplin truncated out of the body's first line. Each candidate carries the
  rule that selected it, so step 8 can measure precision per rule against real
  decisions and retire whichever earns its keep least.
  - Tuned for **precision over recall**: `Standup 2026-01-14` and
    `Untitled thoughts on RabbitMQ` are left alone, because a confident
    suggestion on a note whose title was fine erodes trust faster than a
    mediocre one on a genuinely untitled note.
  - Truncation is only flagged for a *strict* prefix of a long opening line. A
    title equal to the first line is not a defect — a note whose opening line is
    a good heading already has a good title — and a short prefix is coincidence.
  - Only the first 1000 characters of each body are read: the rules need the
    opening line, not megabytes of note text.
  - Candidates carry a `prose_words` count and a `generatable` flag. Measured
    against a real 2,240-note vault, **38% of candidates had no prose at all** —
    bookmarks whose body *is* the URL, and screenshots whose body is one
    `![name](:/id)` embed. Resource indexing is metadata-only and OCR is out of
    scope, so a model handed those would invent something confident and wrong.
    Detection still flags them (they are genuine defects); generation skips them.
- **Enrichment store (step 1 of [docs/enrichment.md](docs/enrichment.md))** — the
  `pkm_sidecar.enrichment` subpackage with `suggestions.sqlite3`, its repository
  and an `[enrichment]` config section. No generation yet, and nothing is written
  back to Joplin. Off by default.
  - A *separate* database beside the index: suggestions regenerate, but the
    accept/reject decisions on them do not, and the index's documented upgrade
    path is "delete the file". The index schema is untouched (still v2), so no
    delete-and-resync is forced on anyone.
  - Suggestion identity is an `input_hash` over the body hash, the state being
    replaced, the model and the prompt version — `notes.body_hash` covers the
    body alone, so keying on it would let a retitle reuse an old rejection.
  - `generation` + `superseded_by` let a corpus re-queue coexist with the row it
    replaces; only the newest is offered for review, and a regenerated payload
    that was already rejected inherits that rejection rather than re-asking.
  - `note_embeddings` is keyed `(note_id, model)` and reuse requires the body
    hash *and* model to match: vectors from different embedding models are not
    comparable, so a body-only lookup would silently corrupt nearest-neighbour
    results after a model change.
  - `warn_if_enrichment_endpoint_is_cleartext` warns when note bodies would
    cross a network in cleartext to Ollama's unauthenticated API (CWE-319).
  - The enrichment `schema.sql` is packaged and asserted in CI's build job, like
    the index schema and templates — it is read at runtime, so absence is an
    install-time break rather than a test failure.

### Changed
- **Dependencies refreshed** (`uv lock --upgrade`), superseding the open
  Dependabot PRs in one pass rather than eight sequential rebases: uvicorn
  0.52.1→0.53.0, pydantic 2.13.4→2.13.5, typer 0.27.1→0.27.2, anyio
  4.14.2→4.15.1, plus dev tooling (ruff 0.16.2→0.16.8, mypy 2.3.0→2.3.1,
  coverage 7.15.4→7.16.1, pre-commit 4.6.1→4.6.2) and transitives. Declared
  floors track what CI exercises: `uvicorn[standard]>=0.53`, `ruff>=0.16.8`.
- `astral-sh/setup-uv` v10.0.1→v10.1.0 in CI. `actions/checkout` is already v7.
- `ruff-pre-commit` rev v0.16.2→v0.16.8, kept in step with the `ruff` pin as the
  config comment requires. Ruff 0.16.8 reports no new findings and reformats
  nothing.
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
  `serve` checks it at startup and rebuilds, and the background loop keeps
  retrying (30s doubling to 15min) for as long as the flag is up, so a recovery
  that fails because Joplin is not up yet is not lost. While the flag is up the
  loop does **not** fall through to incremental sync: that only applies events
  from the cursor forward, and the cursor was cleared by the wipe, so it would
  refresh "last synced" over an index still missing every task, link and tag.
  Recovery is deliberately not gated
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
