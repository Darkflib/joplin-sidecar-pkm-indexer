# Manual test checklist (PRD §19.3, §24)

These scenarios require a **real Joplin** with the Web Clipper service enabled
(Tools → Options → Web Clipper → enable; copy the Authorization token). Not run
in CI. Record the date, sidecar version, and Joplin version with each pass.

Setup:

```sh
export JOPLIN_TOKEN="<your joplin web-clipper token>"
export PKM_SIDECAR_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

## A. Sidecar-only acceptance

- [ ] **doctor** — `uv run pkm-sidecar doctor` shows `OK` for config, db_dir_writable,
      fts5_available, bind_local, joplin_reachable, and joplin_token. Exit code 0.
- [ ] **full rebuild** — `uv run pkm-sidecar index rebuild` completes and prints
      non-zero `notes=`/`folders=`/`tags=` counts. Exit 0.
- [ ] **db inspect** — `sqlite3 "$(uv run pkm-sidecar db path)" "select count(*) from notes;"`
      matches roughly your note count.
- [ ] **serve** — `uv run pkm-sidecar serve` starts; `curl -s localhost:8765/health`
      returns `{"ok":true,...}` in well under 2 s.
- [ ] **search** — pick a word you know exists (e.g. a tool name); 
      `curl -s -H "Authorization: Bearer $PKM_SIDECAR_API_TOKEN" "localhost:8765/api/search?q=<word>"`
      returns the matching note with a snippet.
- [ ] **views** — `recent`, `inbox`, `untagged`, `todos`, `stale` each return
      sensible lists (authenticated).
- [ ] **dashboard** — `uv run pkm-sidecar open` launches the browser; sections
      populate; the address bar shows no `#token=` after load (stripped by JS).
- [ ] **incremental** — edit a note in Joplin, wait one poll interval (or run
      `index sync`), then search for the new content — it appears; `body_hash`
      changed; `/api/status` shows an advanced `last_event_id`.
- [ ] **degraded** — quit Joplin, reload the dashboard: it still loads from the
      stale index and shows the “Joplin offline” badge; `/api/status` reports
      `joplin.reachable=false`.
- [ ] **no mutation** — confirm via Joplin that no notes were created, edited,
      moved, or deleted during any of the above.
- [ ] **secret hygiene** — grep the captured logs: the Joplin token and the
      sidecar API token never appear (even at `--log-level DEBUG`).

## B. Launcher integration smoke (joplin-sidecar-launcher v0.1.1)

See [docs/launcher.md](../../docs/launcher.md) for the exact Command/Arguments.

- [ ] **launch** — configure the launcher (Command `uv`, Arguments
      `["run","--project","<repo>","pkm-sidecar","serve","--host","127.0.0.1","--port","8765"]`,
      Health URL `http://127.0.0.1:8765/health`) and start it from the plugin panel.
- [ ] **health** — the launcher panel shows the sidecar as `healthy` within a few
      seconds.
- [ ] **launched_by** — `/api/status` `runtime.launched_by` reports
      `joplin-sidecar-launcher`.
- [ ] **stop** — stopping from the panel shuts the sidecar down within 3 s
      (clean SIGTERM, no SIGKILL needed).
- [ ] **diag safety** — the launcher’s “Copy diag” buffer contains no token or
      note-body text.
- [ ] **token source** — confirm the token came from the config file or a wrapper
      script (the launcher has no env-var UI). See docs/launcher.md.
