"""Tests for logging setup, log_event, retry, and LogOnceGuard (PRD §16, §17)."""

import logging
import re

import httpx
import pytest

from pkm_sidecar.logging_config import (
    LogOnceGuard,
    configure_logging,
    get_logger,
    log_event,
    reset_for_tests,
    retry_with_backoff,
)


@pytest.fixture(autouse=True)
def _clean_logging():
    reset_for_tests()
    yield
    reset_for_tests()


class TestConfigure:
    def test_idempotent_single_handler(self) -> None:
        configure_logging("INFO")
        n = len(logging.getLogger().handlers)
        configure_logging("DEBUG")
        configure_logging("WARNING")
        assert len(logging.getLogger().handlers) == n

    def test_get_logger_namespace(self) -> None:
        assert get_logger("indexer").name == "pkm_sidecar.indexer"

    def test_format_is_iso8601_utc(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO")
        get_logger("fmt").info("hello world")
        err = capsys.readouterr().err
        # e.g. 2026-05-31T09:00:00.123Z INFO pkm_sidecar.fmt hello world
        assert re.search(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z INFO pkm_sidecar\.fmt hello world",
            err,
        )


class TestLogEvent:
    def test_emits_event_with_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        configure_logging("DEBUG")
        log = get_logger("indexer")
        with caplog.at_level(logging.INFO):
            log_event(log, "index.full.completed", notes=42, mode="rebuild")
        msg = caplog.records[-1].getMessage()
        assert msg.startswith("index.full.completed")
        assert "notes=42" in msg
        assert "mode=rebuild" in msg

    def test_quotes_values_with_spaces(self, caplog: pytest.LogCaptureFixture) -> None:
        configure_logging("DEBUG")
        log = get_logger("indexer")
        with caplog.at_level(logging.INFO):
            log_event(log, "joplin.unreachable", reason="connection refused")
        msg = caplog.records[-1].getMessage()
        assert "reason='connection refused'" in msg

    def test_drops_forbidden_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        configure_logging("DEBUG")
        log = get_logger("indexer")
        with caplog.at_level(logging.INFO):
            log_event(log, "index.full.completed", body="secret note text", token="abc")
        event_lines = [
            r.getMessage() for r in caplog.records if "index.full.completed" in r.getMessage()
        ]
        assert event_lines
        assert "secret note text" not in event_lines[0]
        assert "token=" not in event_lines[0]


class TestRetryWithBackoff:
    async def test_succeeds_first_try(self) -> None:
        calls = 0

        async def fn() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result = await retry_with_backoff(
            fn,
            retry_on=(httpx.ConnectError,),
            logger=get_logger("test"),
            op="x",
        )
        assert result == "ok"
        assert calls == 1

    async def test_retries_then_succeeds(self) -> None:
        calls = 0

        async def fn() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("boom")
            return "ok"

        result = await retry_with_backoff(
            fn,
            retries=3,
            base_delay=0.0,
            jitter=0.0,
            retry_on=(httpx.ConnectError,),
            logger=get_logger("test"),
            op="x",
        )
        assert result == "ok"
        assert calls == 3

    async def test_reraises_after_budget(self) -> None:
        async def fn() -> str:
            raise httpx.ConnectError("always")

        with pytest.raises(httpx.ConnectError):
            await retry_with_backoff(
                fn,
                retries=2,
                base_delay=0.0,
                jitter=0.0,
                retry_on=(httpx.ConnectError,),
                logger=get_logger("test"),
                op="x",
            )

    async def test_does_not_retry_unlisted_exception(self) -> None:
        calls = 0

        async def fn() -> str:
            nonlocal calls
            calls += 1
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await retry_with_backoff(
                fn,
                retries=3,
                base_delay=0.0,
                jitter=0.0,
                retry_on=(httpx.ConnectError,),
                logger=get_logger("test"),
                op="x",
            )
        assert calls == 1

    async def test_cancelled_error_not_retried(self) -> None:
        calls = 0

        async def fn() -> str:
            nonlocal calls
            calls += 1
            raise TimeoutError("cancel-stand-in")

        # Even with a broad retry_on, CancelledError must propagate. We assert the
        # explicit guard by using a retry_on that would otherwise catch broadly.
        import asyncio

        async def cancel_fn() -> str:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await retry_with_backoff(
                cancel_fn,
                retries=5,
                base_delay=0.0,
                jitter=0.0,
                retry_on=(Exception,),
                logger=get_logger("test"),
                op="x",
            )


class TestLogOnceGuard:
    def test_first_call_allowed_then_suppressed(self) -> None:
        guard = LogOnceGuard()
        assert guard.should_log("joplin.token_invalid", cooldown_seconds=1000) is True
        assert guard.should_log("joplin.token_invalid", cooldown_seconds=1000) is False

    def test_distinct_keys_independent(self) -> None:
        guard = LogOnceGuard()
        assert guard.should_log("a", cooldown_seconds=1000) is True
        assert guard.should_log("b", cooldown_seconds=1000) is True

    def test_reset_reallows(self) -> None:
        guard = LogOnceGuard()
        assert guard.should_log("a", cooldown_seconds=1000) is True
        guard.reset()
        assert guard.should_log("a", cooldown_seconds=1000) is True
