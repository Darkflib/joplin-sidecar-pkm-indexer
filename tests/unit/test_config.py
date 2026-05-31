"""Unit tests for configuration loading and precedence (PRD §7)."""

from pathlib import Path

import pytest

from pkm_sidecar.config import (
    AppConfig,
    is_loopback_host,
    load_config,
    redact,
    require_joplin_token,
)
from pkm_sidecar.errors import ConfigError


def _load(
    tmp_path: Path, *, env: dict[str, str] | None = None, cli=None, write_toml: str | None = None
):
    """Load config with an isolated DB/config dir and an empty base env."""
    base_env = {
        "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
        "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
    }
    # Always materialise the config file (empty when no TOML is requested) so the
    # explicit config path resolves and tests never touch the real ~/.config.
    cfg_path = tmp_path / "cfg" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(write_toml if write_toml is not None else "")
    base_env.update(env or {})
    return load_config(env=base_env, cli_overrides=cli)


class TestLoopback:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.5"])
    def test_loopback_hosts(self, host: str) -> None:
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "not-an-ip"])
    def test_non_loopback_hosts(self, host: str) -> None:
        assert is_loopback_host(host) is False


class TestDefaults:
    def test_defaults_applied(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path)
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 8765
        assert cfg.server.api_token is None
        assert cfg.joplin.base_url == "http://127.0.0.1:41184"
        assert cfg.joplin.event_poll_seconds == 10
        assert cfg.joplin.page_limit == 100
        assert cfg.logging.level == "INFO"
        assert cfg.indexing.stale_days == 90
        assert cfg.indexing.inbox_folder_names == ["Inbox", "00 Inbox", "_Inbox"]

    def test_data_dir_is_created(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path)
        assert cfg.database.path.parent.is_dir()


class TestEnv:
    def test_env_overrides_defaults(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path,
            env={
                "PKM_SIDECAR_PORT": "9000",
                "JOPLIN_TOKEN": "joptok",
                "JOPLIN_PAGE_LIMIT": "250",
                "PKM_SIDECAR_LOG_LEVEL": "DEBUG",
            },
        )
        assert cfg.server.port == 9000
        assert cfg.joplin.token is not None
        assert cfg.joplin.token.get_secret_value() == "joptok"
        assert cfg.joplin.page_limit == 250
        assert cfg.logging.level == "DEBUG"

    def test_empty_string_token_is_unset(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, env={"JOPLIN_TOKEN": "", "PKM_SIDECAR_API_TOKEN": ""})
        assert cfg.joplin.token is None
        assert cfg.server.api_token is None

    def test_empty_base_url_falls_back_to_default(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, env={"JOPLIN_BASE_URL": ""})
        assert cfg.joplin.base_url == "http://127.0.0.1:41184"

    def test_empty_port_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _load(tmp_path, env={"PKM_SIDECAR_PORT": ""})

    def test_non_integer_port_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _load(tmp_path, env={"PKM_SIDECAR_PORT": "not-a-number"})

    def test_base_url_trailing_slash_stripped(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, env={"JOPLIN_BASE_URL": "http://127.0.0.1:41184/"})
        assert cfg.joplin.base_url == "http://127.0.0.1:41184"


class TestToml:
    def test_toml_sections_mapped(self, tmp_path: Path) -> None:
        toml = """
[server]
port = 7000
[joplin]
base_url = "http://127.0.0.1:55555"
event_poll_seconds = 30
[indexing]
stale_days = 45
inbox_folder_names = ["In", "Triage"]
[logging]
level = "WARNING"
"""
        cfg = _load(tmp_path, write_toml=toml)
        assert cfg.server.port == 7000
        assert cfg.joplin.base_url == "http://127.0.0.1:55555"
        assert cfg.joplin.event_poll_seconds == 30
        assert cfg.indexing.stale_days == 45
        assert cfg.indexing.inbox_folder_names == ["In", "Triage"]
        assert cfg.logging.level == "WARNING"
        assert cfg.config_path is not None

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _load(tmp_path, write_toml="this is = = not toml")

    def test_explicit_missing_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_config(env={"PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "nope.toml")})


class TestPrecedence:
    def test_cli_beats_env_beats_toml(self, tmp_path: Path) -> None:
        toml = "[server]\nport = 1111\n"
        # TOML says 1111, env says 2222, CLI says 3333 -> CLI wins.
        cfg = _load(tmp_path, env={"PKM_SIDECAR_PORT": "2222"}, cli={"port": 3333}, write_toml=toml)
        assert cfg.server.port == 3333

    def test_env_beats_toml_when_no_cli(self, tmp_path: Path) -> None:
        toml = "[server]\nport = 1111\n"
        cfg = _load(tmp_path, env={"PKM_SIDECAR_PORT": "2222"}, write_toml=toml)
        assert cfg.server.port == 2222

    def test_none_cli_override_is_ignored(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, env={"PKM_SIDECAR_PORT": "2222"}, cli={"port": None})
        assert cfg.server.port == 2222


class TestBindSafety:
    # Enforcement moved to serve/security (exit 3); load_config just records the host.
    def test_load_accepts_non_localhost_host(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, env={"PKM_SIDECAR_HOST": "0.0.0.0"})
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.allow_non_localhost is False

    def test_allow_non_localhost_flag_recorded(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path, env={"PKM_SIDECAR_HOST": "0.0.0.0"}, cli={"allow_non_localhost": True}
        )
        assert cfg.server.allow_non_localhost is True


class TestSecrets:
    def test_tokens_are_redacted_in_repr_and_dump(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path, env={"JOPLIN_TOKEN": "supersecret", "PKM_SIDECAR_API_TOKEN": "apisecret"}
        )
        assert "supersecret" not in repr(cfg)
        assert "apisecret" not in repr(cfg)
        dumped = str(cfg.model_dump())
        assert "supersecret" not in dumped
        assert "apisecret" not in dumped

    def test_redact_helper(self) -> None:
        from pydantic import SecretStr

        assert redact(None) == "unset"
        assert redact("") == "unset"
        assert redact("x") == "set"
        assert redact(SecretStr("x")) == "set"


class TestRequireJoplinToken:
    def test_raises_when_missing(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path)
        with pytest.raises(ConfigError, match="Joplin token is required"):
            require_joplin_token(cfg)

    def test_returns_value_when_present(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, env={"JOPLIN_TOKEN": "joptok"})
        assert require_joplin_token(cfg) == "joptok"


class TestImmutability:
    def test_appconfig_is_frozen(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path)
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on frozen set
            cfg.server.port = 1  # type: ignore[misc]
        assert isinstance(cfg, AppConfig)
