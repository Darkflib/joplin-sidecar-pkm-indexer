"""Smoke tests for the CLI (PRD §15, §18.1/2). Network-free paths only.

`serve` is not started (it blocks); we test its bind-safety gate. Commands that
would hit Joplin are tested via their config-error/exit-code paths.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pkm_sidecar import __version__, logging_config
from pkm_sidecar.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_logging():
    logging_config.reset_for_tests()
    yield
    logging_config.reset_for_tests()


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.toml").write_text("")
    env = {
        "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
        "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
        "PKM_SIDECAR_RUNTIME_DIR": str(tmp_path / "rt"),
    }
    env.update(extra)
    return env


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_db_path_prints_only_path(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = runner.invoke(app, ["db", "path"], env=env)
    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path / "data" / "index.sqlite3")


def test_status_json(tmp_path: Path) -> None:
    import json

    env = _env(tmp_path, JOPLIN_TOKEN="tok")
    result = runner.invoke(app, ["status", "--json"], env=env)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["service"] == "pkm-sidecar"
    assert payload["database"]["note_count"] == 0
    assert "token" not in payload["joplin"]  # token never present


def test_index_rebuild_without_token_is_config_error(tmp_path: Path) -> None:
    env = _env(tmp_path)  # no JOPLIN_TOKEN
    result = runner.invoke(app, ["index", "rebuild"], env=env)
    assert result.exit_code == 2  # config error


def test_index_sync_without_token_is_config_error(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = runner.invoke(app, ["index", "sync"], env=env)
    assert result.exit_code == 2


def test_serve_rejects_non_localhost_without_flag(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"], env=env)
    assert result.exit_code == 3  # security gate, not config


def test_doctor_runs_and_emits_check_lines(tmp_path: Path) -> None:
    env = _env(tmp_path, JOPLIN_TOKEN="tok")
    result = runner.invoke(app, ["doctor"], env=env)
    # Exit depends on environment (Joplin reachability/token); just assert it ran.
    assert result.exit_code in (0, 1)
    assert "config" in result.stdout
    assert "fts5_available" in result.stdout
    assert "bind_local" in result.stdout


def test_open_uses_browser_and_hides_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: dict[str, str] = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url) is None)
    env = _env(tmp_path, PKM_SIDECAR_API_TOKEN="my-secret-token")
    result = runner.invoke(app, ["open"], env=env)
    assert result.exit_code == 0
    assert "#token=my-secret-token" in opened["url"]  # handed off via fragment
    assert "my-secret-token" not in result.stdout  # never printed
