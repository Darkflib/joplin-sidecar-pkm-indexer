"""Tests for token redaction in logs (PRD §8.3, §16, §21)."""

import logging

import pytest

from pkm_sidecar.logging_config import (
    TokenRedactionFilter,
    add_secret,
    configure_logging,
    get_logger,
    reset_for_tests,
)

TOKEN = "supersecret-joplin-token-abcdef123456"


@pytest.fixture(autouse=True)
def _clean_logging():
    reset_for_tests()
    yield
    reset_for_tests()


class TestFilterUnit:
    def _record(self, msg: str, args=None) -> logging.LogRecord:
        return logging.LogRecord("pkm_sidecar.test", logging.INFO, __file__, 1, msg, args, None)

    def test_redacts_msg(self) -> None:
        f = TokenRedactionFilter()
        f.add_secret(TOKEN)
        rec = self._record(f"using {TOKEN} now")
        f.filter(rec)
        assert TOKEN not in rec.getMessage()
        assert "***REDACTED***" in rec.getMessage()

    def test_redacts_args(self) -> None:
        f = TokenRedactionFilter()
        f.add_secret(TOKEN)
        rec = self._record("token=%s", (TOKEN,))
        f.filter(rec)
        assert TOKEN not in rec.getMessage()

    def test_redacts_extra_string_attrs(self) -> None:
        f = TokenRedactionFilter()
        f.add_secret(TOKEN)
        rec = self._record("hello")
        rec.custom = f"embedded {TOKEN}"  # type: ignore[attr-defined]
        f.filter(rec)
        assert TOKEN not in rec.custom  # type: ignore[attr-defined]

    def test_redacts_authorization_bearer_unconditionally(self) -> None:
        f = TokenRedactionFilter()  # no secret registered
        rec = self._record("Authorization: Bearer abc.def.ghi")
        f.filter(rec)
        assert "abc.def.ghi" not in rec.getMessage()
        assert "***REDACTED***" in rec.getMessage()

    def test_empty_secret_not_registered(self) -> None:
        f = TokenRedactionFilter()
        f.add_secret("")
        rec = self._record("nothing to redact")
        f.filter(rec)
        assert rec.getMessage() == "nothing to redact"


class TestConfiguredLoggingRedaction:
    def test_token_never_appears_in_caplog(self, caplog: pytest.LogCaptureFixture) -> None:
        configure_logging("DEBUG", redact_values=[TOKEN])
        log = get_logger("client")
        with caplog.at_level(logging.DEBUG):
            log.info("connecting with token %s", TOKEN)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in joined
        assert TOKEN not in caplog.text

    def test_token_never_appears_via_exception_path(self, caplog: pytest.LogCaptureFixture) -> None:
        configure_logging("DEBUG", redact_values=[TOKEN])
        log = get_logger("client")
        with caplog.at_level(logging.DEBUG):
            try:
                raise ValueError(f"boom {TOKEN}")
            except ValueError:
                log.exception("request failed")
        all_text = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
        for rec in caplog.records:
            if rec.exc_text:
                all_text += rec.exc_text
        assert TOKEN not in all_text

    def test_add_secret_registers_runtime_token(self, caplog: pytest.LogCaptureFixture) -> None:
        configure_logging("DEBUG")
        ephemeral = "ephemeral-api-token-zzz999"
        add_secret(ephemeral)
        log = get_logger("security")
        with caplog.at_level(logging.DEBUG):
            log.info("minted %s", ephemeral)
        assert ephemeral not in "\n".join(r.getMessage() for r in caplog.records)
