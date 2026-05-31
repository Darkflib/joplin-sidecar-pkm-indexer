# Definition-of-Done verification — v0.1.0

Run: 2026-05-31 · sidecar 0.1.0 · against a live Joplin (Web Clipper API on
127.0.0.1:41184). Executed with a throwaway temp DB/config (no real paths
touched). Tokens supplied via a git-ignored `.env`; never printed.

Index scale observed: **2205 notes, 85 folders, 41 tags, 255 extracted tasks,
15477 extracted links**; full rebuild in ~3 s, 0 errors.

| PRD §24 criterion | Result |
|---|---|
| package installs/runs locally | ✅ `uv sync`, `pkm-sidecar --version` |
| `doctor` works | ✅ all OK, `joplin_token: accepted` |
| `index rebuild` indexes real Joplin data | ✅ 2205 notes, run status `success` |
| `serve` starts a local API | ✅ binds 127.0.0.1, clean SIGTERM shutdown |
| `/health` works without auth | ✅ `{"ok":true,…}` |
| `/api/status` works with auth | ✅ counts + cursors; 401 without/with bad token |
| `/api/search` returns FTS results | ✅ `Ideabrowser` → note + snippet |
| recent/inbox/untagged/TODO/stale views | ✅ recent/untagged/stale return data, todos=10; inbox=0 (no folder named Inbox/00 Inbox/_Inbox — configurable) |
| incremental sync updates changed notes | ✅ background loop advanced `last_event_id` to 4541 live; unit/integration cover body-change |
| tolerates Joplin temporarily unavailable | ✅ status serves DB counts independent of Joplin; integration degraded test |
| tests pass | ✅ 280 automated, 91% coverage |
| README documents setup/risks/limitations | ✅ README + docs/launcher.md |
| no Joplin mutation endpoints exist | ✅ AST scan + live no-mutation (only GET requests issued) |

## Bug found and fixed during this run

Real Joplin returns notes whose `parent_id` references a folder absent from
`/folders` (Trash/Conflicts), which tripped a `FOREIGN KEY constraint failed` on
insert. Since the index only soft-deletes (FK `ON DELETE` actions never fire), the
`notes.parent_id→folders` and `note_tags` FKs only imposed spurious insert-time
failures. Removed those FKs (kept the always-satisfiable `extracted_*→notes` FKs);
views already tolerate missing parents via JOINs. Regression tests added.

## Notes

- `inbox` is empty because this vault has no folder named in the default
  `inbox_folder_names`. Set `[indexing].inbox_folder_names` to match your vault.
- The token never appeared in any captured stdout/stderr (redaction filter).
