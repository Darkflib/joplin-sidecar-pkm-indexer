# Launcher integration

How to run pkm-sidecar from the **Joplin Sidecar Launcher** plugin (tested against
v0.1.1). The launcher spawns a child process, polls a health URL, and shows
recent stdout/stderr. No code changes to the launcher are needed.

## Recommended settings

In Joplin → **Settings → Sidecar Launcher**:

| Setting | Value |
|---|---|
| Command | absolute path to `uv` (e.g. `/opt/homebrew/bin/uv` — run `which uv`) |
| Arguments (JSON) | `["run","--project","<ABSOLUTE PATH TO THIS REPO>","pkm-sidecar","serve","--host","127.0.0.1","--port","8765"]` |
| Working directory | `<ABSOLUTE PATH TO THIS REPO>` |
| Health check URL | `http://127.0.0.1:8765/health` |
| Autostart on plugin load | enable **after** a successful manual run |
| Stop sidecar when plugin unloads | enabled |

`uv run --project <repo>` is the most robust form: it ignores any inherited
`VIRTUAL_ENV`/`PYTHONPATH` (which the launcher strips anyway) and resolves the
right environment regardless of the shell Joplin inherited.

### Alternatives

- **Installed as a tool** (`uv tool install .`): Command `uv`, Arguments
  `["tool","run","pkm-sidecar","serve","--host","127.0.0.1","--port","8765"]`,
  working directory blank.
- **Installed via pipx** (on `PATH`): Command `pkm-sidecar`, Arguments
  `["serve","--host","127.0.0.1","--port","8765"]`.

## Supplying tokens (the launcher has no env-var UI)

`JOPLIN_TOKEN` and `PKM_SIDECAR_API_TOKEN` must reach the sidecar another way.
In order of preference:

1. **Config file (recommended).** Put them in
   `~/.config/pkm-sidecar/config.toml` and `chmod 600` it. The sidecar reads it
   automatically. Self-contained and independent of how Joplin was launched.
2. **Wrapper script.** Set Command to a small user-owned script that exports the
   vars and `exec`s `uv run … serve …`. Keep the script `chmod 700`.
3. **Login-shell environment (fragile).** If the Joplin desktop process inherits
   your shell env, exported vars are visible. This breaks when Joplin is launched
   from a GUI/launchd context that doesn't load your shell profile — don't rely
   on it.

> The launcher strips `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`, `VIRTUAL_ENV`,
> and `CONDA_PREFIX` before spawning, and injects
> `JOPLIN_SIDECAR_LAUNCHED_BY=joplin-sidecar-launcher`. The sidecar surfaces the
> latter at `/api/status` → `runtime.launched_by`.

## Contracts the sidecar already satisfies

- **Health:** `GET /health` is public, lock-free, and returns
  `{"ok":true,"service":"pkm-sidecar","version":"…"}` well within the launcher's
  2000 ms timeout — even during a full rebuild (indexing yields the event loop
  between paginated batches).
- **Shutdown:** clean exit on `SIGTERM` within 3 s (uvicorn's handler plus the
  indexer's deadline-bounded stop), so the launcher never has to `SIGKILL`.
- **Log hygiene:** line-buffered logs that never contain the Joplin token, the
  API token, or note bodies — safe for the launcher's "Copy diag" clipboard.

## Foot-guns

- **Port:** the Health URL hard-codes the port. If you change
  `PKM_SIDECAR_PORT`, update the Health URL to match.
- **First run:** run `uv run pkm-sidecar doctor` and a manual `serve` from a
  terminal once before enabling autostart, so token/path problems surface with a
  clear message rather than a silent crash loop.
- **Non-localhost:** the launcher rejects non-localhost health URLs. Keep the
  sidecar on `127.0.0.1`; do **not** configure `--allow-non-localhost` for
  launcher use.
- **Windows:** experimental. The token file's `0600` permission is a no-op on
  Windows; prefer the config-file path with NTFS ACLs you control.
