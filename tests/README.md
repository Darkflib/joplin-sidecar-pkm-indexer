# Test suite

```sh
uv run pytest -m 'not manual' --cov=pkm_sidecar --cov-report=term-missing --cov-fail-under=70
```

- **unit** (`tests/unit/`) — pure logic: config precedence, redaction, errors,
  markdown extraction, db schema/FTS, repositories, the Joplin client.
- **integration** (`tests/integration/`) — the real FastAPI app + a mocked Joplin
  (`tests/_fake_joplin.py`, an in-memory store over `httpx.MockTransport`) + a
  temp SQLite database.
- **manual** (`tests/manual/CHECKLIST.md`) — scenarios that need a real Joplin;
  not automated. Excluded from CI via `-m 'not manual'`.

## PRD §18 acceptance criteria → tests

| Criterion | Test |
|---|---|
| 18.1 service starts / `/health` | `integration/test_api_status_health.py::test_health_no_auth`, `integration/test_api_public.py`, `integration/test_cli_smoke.py` (serve booted live; see manual) |
| 18.2 doctor works | `integration/test_cli_smoke.py::test_doctor_runs_and_emits_check_lines` |
| 18.3 full rebuild works | `integration/test_full_rebuild.py::test_full_rebuild_indexes_all_entities` |
| 18.4 search works | `integration/test_api_search.py::test_search_returns_hit_with_snippet` |
| 18.5 recent view | `integration/test_api_views.py::test_recent_ordered_desc` |
| 18.6 untagged **and inbox** views | `integration/test_api_views.py::test_untagged`, `::test_inbox` |
| 18.7 markdown task extraction | `unit/test_markdown_extract.py::TestTasks`, `integration/test_full_rebuild.py` (tasks populated) |
| 18.8 incremental sync | `integration/test_incremental_sync.py::test_incremental_updates_changed_note` |
| 18.9 Joplin outage / degraded | `integration/test_degraded_mode.py` |
| 18.10 no mutation | `integration/test_no_mutation.py::test_only_get_requests_across_all_flows`, `unit/test_joplin_client_readonly.py::test_no_mutating_httpx_calls_in_source` |

## Coverage targets

Total ≥70% (CI gate). Core modules (config, security, markdown_extract,
repositories, indexer, services, joplin_client, db) sit ≥85%; `dashboard.py` and
`cli.py` are smoke-covered (their real exercise is the manual checklist).
