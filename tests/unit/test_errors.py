"""Unit tests for the canonical exception hierarchy (PRD §17)."""

import pytest

from pkm_sidecar import errors
from pkm_sidecar.errors import (
    AuthError,
    ConflictError,
    DatabaseLockedError,
    IndexInProgressError,
    JoplinAuthError,
    JoplinCursorInvalidError,
    JoplinError,
    JoplinNotFoundError,
    JoplinRateLimitedError,
    JoplinUnreachableError,
    NotFoundError,
    PkmSidecarError,
    scrub_token,
    to_http_status,
)


class TestScrubToken:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://h/notes?token=abc123", "http://h/notes?token=***"),
            ("http://h/notes?fields=id&token=abc123", "http://h/notes?fields=id&token=***"),
            ("http://h/notes?token=abc123&fields=id", "http://h/notes?token=***&fields=id"),
            ("http://h/notes?TOKEN=abc123", "http://h/notes?TOKEN=***"),  # case-insensitive
            ("http://h/notes?fields=id", "http://h/notes?fields=id"),  # nothing to scrub
        ],
    )
    def test_scrubs_token_query_param(self, raw: str, expected: str) -> None:
        assert scrub_token(raw) == expected

    def test_does_not_touch_unrelated_text(self) -> None:
        assert scrub_token("no secrets here") == "no secrets here"


class TestJoplinErrorScrubbing:
    def test_url_is_scrubbed_on_construction(self) -> None:
        exc = JoplinError("boom", url="http://127.0.0.1:41184/notes?token=supersecret")
        assert exc.url == "http://127.0.0.1:41184/notes?token=***"
        assert "supersecret" not in str(exc.url)

    def test_str_repr_never_leaks_token(self) -> None:
        exc = JoplinUnreachableError("down", url="http://h/x?token=leakme", status_code=None)
        assert "leakme" not in repr(exc)
        assert "leakme" not in str(exc.url)

    def test_url_optional(self) -> None:
        exc = JoplinError("no url")
        assert exc.url is None
        assert exc.status_code is None


class TestHierarchy:
    def test_all_inherit_base(self) -> None:
        for klass in [
            AuthError,
            NotFoundError,
            ConflictError,
            IndexInProgressError,
            DatabaseLockedError,
            JoplinError,
            JoplinAuthError,
            JoplinUnreachableError,
        ]:
            assert issubclass(klass, PkmSidecarError)

    def test_joplin_leaves_are_joplin_errors(self) -> None:
        for klass in [
            JoplinUnreachableError,
            JoplinAuthError,
            JoplinNotFoundError,
            JoplinRateLimitedError,
            JoplinCursorInvalidError,
        ]:
            assert issubclass(klass, JoplinError)

    def test_index_in_progress_is_a_conflict(self) -> None:
        assert issubclass(IndexInProgressError, ConflictError)


class TestAuthError:
    def test_default_reason_is_missing(self) -> None:
        assert AuthError().reason == "missing"

    def test_reason_is_recorded(self) -> None:
        assert AuthError("nope", reason="invalid").reason == "invalid"


class TestToHttpStatus:
    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (AuthError(), 401),
            (NotFoundError(), 404),
            (JoplinNotFoundError("x"), 404),
            (ConflictError(), 409),
            (IndexInProgressError(), 409),
            (JoplinAuthError("x"), 502),
            (JoplinUnreachableError("x"), 503),
            (JoplinRateLimitedError("x"), 503),
            (DatabaseLockedError(), 503),
            (PkmSidecarError(), 500),
            (JoplinError("x"), 500),
            (JoplinCursorInvalidError("x"), 500),
        ],
    )
    def test_status_table(self, exc: PkmSidecarError, status: int) -> None:
        assert to_http_status(exc) == status

    def test_unknown_subclass_falls_back_to_parent(self) -> None:
        class WeirdAuth(AuthError):
            pass

        assert to_http_status(WeirdAuth()) == 401

    def test_all_exported_names_present(self) -> None:
        for name in errors.__all__:
            assert hasattr(errors, name)
