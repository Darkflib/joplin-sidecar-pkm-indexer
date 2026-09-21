"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest

from pkm_sidecar import services


@pytest.fixture(autouse=True)
def _reset_failure_log_guard() -> Iterator[None]:
    """Clear the process-global incremental-failure log guard around every test.

    ``services._FAILURE_LOG_GUARD`` deliberately spans ticks (that is what makes
    it rate-limit a sustained outage) and therefore also spans tests: without
    this, the first test to log a given error type silences every later one for
    five minutes, and assertions about log volume pass or fail depending on test
    ordering.
    """
    services._FAILURE_LOG_GUARD.reset()
    yield
    services._FAILURE_LOG_GUARD.reset()
