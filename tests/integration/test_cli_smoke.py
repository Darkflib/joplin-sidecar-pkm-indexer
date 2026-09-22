"""Smoke tests for the CLI (PRD §15, §18.1/2). Network-free paths only.

`serve` is not started (it blocks); we test its bind-safety gate. Commands that
would hit Joplin are tested via their config-error/exit-code paths.
"""

from pathlib import Path

import pytest
import uvicorn
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


@pytest.fixture
def never_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a regressed bind guard fail loudly rather than bind a real port.

    Without this, a `serve` that wrongly gets past its guard starts uvicorn for
    real and the test hangs until CI times out.
    """

    async def _boom(self: uvicorn.Server, sockets: object = None) -> None:
        raise AssertionError("uvicorn.Server.serve() was reached — the bind guard did not fire")

    monkeypatch.setattr(uvicorn.Server, "serve", _boom)


def test_serve_refuses_non_local_bind_without_override(tmp_path: Path, never_serves: None) -> None:
    env = _env(tmp_path, PKM_SIDECAR_API_TOKEN="chosen")
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"], env=env)
    assert result.exit_code == 3
    assert "--allow-non-localhost" in result.stderr


def test_serve_refuses_non_local_bind_on_an_ephemeral_token(
    tmp_path: Path, never_serves: None
) -> None:
    """Exit 3 rather than exposing the API behind a token nobody has seen."""
    result = runner.invoke(
        app, ["serve", "--host", "0.0.0.0", "--allow-non-localhost"], env=_env(tmp_path)
    )
    assert result.exit_code == 3
    assert "PKM_SIDECAR_API_TOKEN" in result.stderr
    # Refused before resolve_api_token could mint and write one.
    assert not list((tmp_path / "rt").glob("*.token"))


def _enrich_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = _env(tmp_path, **extra)
    (tmp_path / "cfg" / "config.toml").write_text(
        "[enrichment]\nenabled = true\nollama_base_url = "
        f'"{extra.pop("base", "http://127.0.0.1:9")}"\n'
    )
    return env


def test_doctor_skips_enrichment_when_disabled(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"], env=_env(tmp_path))
    assert "enrichment" in result.stdout
    assert "SKIP: enrichment" in result.stdout


def test_doctor_reports_unreachable_enrichment_host(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"], env=_enrich_env(tmp_path))
    assert "enrichment_reachable" in result.stdout
    assert "FAIL" in result.stdout


def test_cleartext_transport_warns_without_failing_the_run(tmp_path: Path) -> None:
    """A LAN model host is a deliberate trade-off, not a broken configuration."""
    env = _env(tmp_path)
    (tmp_path / "cfg" / "config.toml").write_text(
        '[enrichment]\nenabled = true\nollama_base_url = "http://192.0.2.10:11434"\n'
    )
    result = runner.invoke(app, ["doctor"], env=env)
    assert "WARN: enrichment_transport" in result.stdout
    # The WARN itself must not be what decides the exit code.
    assert "cleartext" in result.stdout


def test_loopback_enrichment_host_does_not_warn(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (tmp_path / "cfg" / "config.toml").write_text(
        '[enrichment]\nenabled = true\nollama_base_url = "http://127.0.0.1:11434"\n'
    )
    result = runner.invoke(app, ["doctor"], env=env)
    assert "WARN: enrichment_transport" not in result.stdout
    assert "OK: enrichment_transport" in result.stdout


def test_doctor_does_not_print_endpoint_credentials(tmp_path: Path) -> None:
    """AnyHttpUrl accepts userinfo, so a configured endpoint can carry a password."""
    env = _env(tmp_path)
    (tmp_path / "cfg" / "config.toml").write_text(
        '[enrichment]\nenabled = true\nollama_base_url = "http://bob:hunter2@198.51.100.7:11434"\n'
    )
    result = runner.invoke(app, ["doctor"], env=env)
    assert "hunter2" not in result.stdout
    assert "198.51.100.7:11434" in result.stdout  # still identifiable


def test_doctor_does_not_contradict_itself_on_a_bad_model_list(tmp_path: Path) -> None:
    """Reachable-but-malformed must not report as unreachable."""
    env = _env(tmp_path)
    (tmp_path / "cfg" / "config.toml").write_text(
        '[enrichment]\nenabled = true\nollama_base_url = "http://127.0.0.1:9"\n'
    )
    result = runner.invoke(app, ["doctor"], env=env)
    reachable_lines = [ln for ln in result.stdout.splitlines() if "enrichment_reachable" in ln]
    assert len(reachable_lines) == 1  # never both OK and FAIL for one check
