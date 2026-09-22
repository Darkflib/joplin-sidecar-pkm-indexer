"""Unit tests for security primitives (PRD §8)."""

import logging
import os
import stat
from pathlib import Path

import httpx
import pytest

from pkm_sidecar import logging_config, security
from pkm_sidecar.config import AppConfig, load_config
from pkm_sidecar.errors import AuthError, SecurityError
from pkm_sidecar.security import (
    SingleOriginTransport,
    assert_non_local_bind_has_token,
    assert_url_matches_base,
    constant_time_compare,
    ephemeral_token_file_path,
    generate_ephemeral_token,
    resolve_api_token,
    validate_bind_address,
    verify_bearer_token,
    warn_if_world_readable_config,
    write_token_file,
)

LOG = logging.getLogger("pkm_sidecar.test")


@pytest.fixture(autouse=True)
def _clean_logging():
    logging_config.reset_for_tests()
    yield
    logging_config.reset_for_tests()


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, env: dict[str, str] | None = None):
    # _runtime_dir() reads the real os.environ, so set the override there too.
    monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    base_env = {
        "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
        "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
    }
    cfg_file = tmp_path / "cfg" / "config.toml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("")
    base_env.update(env or {})
    return load_config(env=base_env)


class TestBindValidation:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_accepted(self, host: str) -> None:
        assert validate_bind_address(host, allow_non_localhost=False) == host

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
    def test_non_loopback_rejected(self, host: str) -> None:
        with pytest.raises(SecurityError):
            validate_bind_address(host, allow_non_localhost=False)

    def test_override_allows_non_loopback(self) -> None:
        assert validate_bind_address("0.0.0.0", allow_non_localhost=True) == "0.0.0.0"


class TestEphemeralToken:
    def test_generates_unique_tokens(self) -> None:
        a, b = generate_ephemeral_token(), generate_ephemeral_token()
        assert a != b
        assert len(a) > 20

    def test_token_file_path_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, monkeypatch, env={"PKM_SIDECAR_PORT": "9999"})
        path = ephemeral_token_file_path(cfg)
        assert path.name == "pkm-sidecar-9999.token"
        assert path.parent == (tmp_path / "runtime")

    def test_write_token_file_atomic_and_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "runtime" / "t.token"
        write_token_file(path, "secret-token")
        assert path.read_text() == "secret-token"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # no leftover temp files
        leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".token-")]
        assert leftovers == []


class TestResolveApiToken:
    def test_configured_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, monkeypatch, env={"PKM_SIDECAR_API_TOKEN": "configured-secret"})
        resolved = resolve_api_token(cfg, LOG)
        assert resolved.source == "config"
        assert resolved.token == "configured-secret"
        # no token file written for configured tokens
        assert not ephemeral_token_file_path(cfg).exists()

    def test_ephemeral_token_minted_and_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, monkeypatch)
        resolved = resolve_api_token(cfg, LOG)
        assert resolved.source == "ephemeral"
        path = ephemeral_token_file_path(cfg)
        assert path.exists()
        assert path.read_text() == resolved.token

    def test_ephemeral_token_value_never_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _config(tmp_path, monkeypatch)
        with caplog.at_level(logging.DEBUG):
            resolved = resolve_api_token(cfg, LOG)
        assert resolved.token not in caplog.text

    def _non_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: str
    ) -> AppConfig:
        # config rejects non-local without override, so supply the override via the CLI layer.
        monkeypatch.setenv("PKM_SIDECAR_RUNTIME_DIR", str(tmp_path / "runtime"))
        (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cfg" / "config.toml").write_text("")
        return load_config(
            env={
                "PKM_SIDECAR_DB_PATH": str(tmp_path / "data" / "index.sqlite3"),
                "PKM_SIDECAR_CONFIG_PATH": str(tmp_path / "cfg" / "config.toml"),
                "PKM_SIDECAR_HOST": "0.0.0.0",
                **extra,
            },
            cli_overrides={"allow_non_localhost": True},
        )

    def test_non_local_bind_with_ephemeral_token_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._non_local_config(tmp_path, monkeypatch)
        with pytest.raises(SecurityError):
            assert_non_local_bind_has_token(cfg)

    def test_non_local_bind_refused_before_a_token_file_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal must land before resolve_api_token mints and writes one."""
        cfg = self._non_local_config(tmp_path, monkeypatch)
        with pytest.raises(SecurityError):
            assert_non_local_bind_has_token(cfg)
        assert not ephemeral_token_file_path(cfg).exists()

    def test_non_local_bind_with_configured_token_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._non_local_config(tmp_path, monkeypatch, PKM_SIDECAR_API_TOKEN="chosen")
        assert_non_local_bind_has_token(cfg)  # no raise

    def test_loopback_bind_with_ephemeral_token_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert_non_local_bind_has_token(_config(tmp_path, monkeypatch))  # no raise


class TestBearerAuth:
    def test_constant_time_compare(self) -> None:
        assert constant_time_compare("abc", "abc") is True
        assert constant_time_compare("abc", "abd") is False

    def test_valid_token_passes(self) -> None:
        verify_bearer_token("Bearer good-token", "good-token")  # no raise

    def test_missing_header(self) -> None:
        with pytest.raises(AuthError) as ei:
            verify_bearer_token(None, "good-token")
        assert ei.value.reason == "missing"

    def test_wrong_scheme(self) -> None:
        with pytest.raises(AuthError) as ei:
            verify_bearer_token("Basic Zm9v", "good-token")
        assert ei.value.reason == "missing"

    def test_invalid_token(self) -> None:
        with pytest.raises(AuthError) as ei:
            verify_bearer_token("Bearer wrong", "good-token")
        assert ei.value.reason == "invalid"

    def test_disabled_when_no_expected_token(self) -> None:
        with pytest.raises(AuthError) as ei:
            verify_bearer_token("Bearer anything", None)
        assert ei.value.reason == "disabled"

    def test_error_never_contains_offered_token(self) -> None:
        with pytest.raises(AuthError) as ei:
            verify_bearer_token("Bearer leaky-offered-token", "good-token")
        assert "leaky-offered-token" not in str(ei.value)


class TestOutboundGuard:
    BASE = "http://127.0.0.1:41184"

    def test_assert_allows_matching_base(self) -> None:
        assert_url_matches_base("http://127.0.0.1:41184/notes?fields=id", self.BASE)

    def test_assert_blocks_foreign_host(self) -> None:
        with pytest.raises(SecurityError):
            assert_url_matches_base("https://evil.example/steal", self.BASE)

    def test_assert_blocks_wrong_port(self) -> None:
        with pytest.raises(SecurityError):
            assert_url_matches_base("http://127.0.0.1:9999/notes", self.BASE)

    async def test_transport_delegates_for_allowed(self) -> None:
        inner = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
        transport = SingleOriginTransport(self.BASE, inner)
        resp = await transport.handle_async_request(
            httpx.Request("GET", "http://127.0.0.1:41184/notes")
        )
        assert resp.status_code == 200

    async def test_transport_blocks_foreign(self) -> None:
        inner = httpx.MockTransport(lambda req: httpx.Response(200))
        transport = SingleOriginTransport(self.BASE, inner)
        with pytest.raises(SecurityError):
            await transport.handle_async_request(httpx.Request("GET", "https://evil.example/"))


class TestConfigPerms:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX perms only")
    def test_warns_on_world_readable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("[server]\n")
        cfg_file.chmod(0o644)
        with caplog.at_level(logging.WARNING):
            warn_if_world_readable_config(cfg_file, LOG)
        assert "chmod 600" in caplog.text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX perms only")
    def test_quiet_when_locked_down(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("[server]\n")
        cfg_file.chmod(0o600)
        with caplog.at_level(logging.WARNING):
            warn_if_world_readable_config(cfg_file, LOG)
        assert "chmod 600" not in caplog.text


class TestRegisterSecret:
    def test_register_secret_redacts(self, caplog: pytest.LogCaptureFixture) -> None:
        logging_config.configure_logging("DEBUG")
        security.register_secret("registered-via-security")
        log = logging_config.get_logger("test")
        with caplog.at_level(logging.DEBUG):
            log.info("value is %s", "registered-via-security")
        assert "registered-via-security" not in "\n".join(r.getMessage() for r in caplog.records)
