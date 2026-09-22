"""Title candidate detection (docs/enrichment.md §5). Pure logic — no model."""

from pathlib import Path

import pytest

from pkm_sidecar import db
from pkm_sidecar.enrichment.title_candidates import (
    MIN_TRUNCATION_LENGTH,
    classify_title,
    count_by_issue,
    find_candidates,
    first_body_line,
    prose_word_count,
)


class TestEmptyAndDefaults:
    @pytest.mark.parametrize("title", ["", "   ", "\t\n"])
    def test_blank(self, title: str) -> None:
        assert classify_title(title) == "empty"

    @pytest.mark.parametrize(
        "title",
        [
            "Untitled",
            "untitled",
            "UNTITLED",
            "Untitled 2",
            "Untitled (3)",
            "New note",
            "New to-do",
            "new todo",
        ],
    )
    def test_joplin_placeholders(self, title: str) -> None:
        assert classify_title(title) == "joplin_default"

    @pytest.mark.parametrize("title", ["Untitled thoughts on RabbitMQ", "New note format for ADRs"])
    def test_placeholder_prefix_with_real_content_is_kept(self, title: str) -> None:
        """'Untitled' as a word is fine; only the bare placeholder is a defect."""
        assert classify_title(title) is None


class TestDateOnly:
    @pytest.mark.parametrize(
        "title",
        [
            "20260114",
            "260114",
            "2026-01-14",
            "2026/01/14",
            "2026.01.14",
            "14/01/2026",
            "14-01-26",
            "2026-01-14 09:30",
            "2026-01-14T09:30:15",
            "14/01/2026 9.30pm",
        ],
    )
    def test_bare_dates(self, title: str) -> None:
        assert classify_title(title) == "date_only"

    @pytest.mark.parametrize(
        "title",
        ["Standup 2026-01-14", "2026-01-14 retro with William", "Q1 2026 planning", "2026 goals"],
    )
    def test_dates_with_words_are_kept(self, title: str) -> None:
        """Weak, but flagging these costs precision — the thing that erodes trust."""
        assert classify_title(title) is None


class TestUrlsAndFilenames:
    @pytest.mark.parametrize(
        "title",
        [
            "https://docs.example.com/guide",
            "http://example.com",
            "www.example.com/x",
            "example.com",
            "docs.example.co.uk/guide",
        ],
    )
    def test_urls(self, title: str) -> None:
        assert classify_title(title) == "url"

    @pytest.mark.parametrize(
        "title", ["invoice-2026.pdf", "Notes.docx", "budget.xlsx", "screenshot.png", "export.csv"]
    )
    def test_filenames(self, title: str) -> None:
        assert classify_title(title) == "filename"

    @pytest.mark.parametrize(
        "title", ["Reading example.com for the API docs", "The .gitignore situation", "Rate limits"]
    )
    def test_prose_mentioning_a_domain_is_kept(self, title: str) -> None:
        assert classify_title(title) is None


class TestTruncatedFromBody:
    def test_strict_prefix_of_a_long_first_line_is_flagged(self) -> None:
        body = (
            "Spent the morning chasing why the accounting workers were double-counting "
            "spend across the ledger.\n\nMore detail follows."
        )
        title = body[:72]
        assert len(title) >= MIN_TRUNCATION_LENGTH
        assert classify_title(title, body) == "truncated_from_body"

    def test_title_equal_to_the_first_line_is_kept(self) -> None:
        """A note whose opening line is a good heading already has a good title."""
        body = "# RabbitMQ ack ordering causes double counting\n\nDetail here."
        assert classify_title("RabbitMQ ack ordering causes double counting", body) is None

    def test_short_prefix_is_kept(self) -> None:
        """'RabbitMQ' over a body starting 'RabbitMQ notes' is coincidence, not truncation."""
        assert classify_title("RabbitMQ", "RabbitMQ notes from the migration") is None

    def test_unrelated_title_is_kept(self) -> None:
        assert classify_title("Ledger idempotency", "Spent the morning chasing a bug") is None

    def test_empty_body_cannot_flag(self) -> None:
        assert classify_title("A perfectly ordinary title that is quite long indeed", "") is None


class TestFirstBodyLine:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("# Heading\n\nbody", "Heading"),
            ("\n\n  ## Second level  \nmore", "Second level"),
            ("> quoted opening\nrest", "quoted opening"),
            ("- bullet first\nrest", "bullet first"),
            ("plain line\nrest", "plain line"),
            ("", ""),
            ("\n \n", ""),
        ],
    )
    def test_extraction(self, body: str, expected: str) -> None:
        assert first_body_line(body) == expected


@pytest.fixture
def index(tmp_path: Path):
    path = tmp_path / "index.sqlite3"
    db.init_db(path)
    conn = db.open_writer_connection(path)
    yield conn
    conn.close()


def _add(conn, note_id: str, title: str, body: str = "", *, deleted: int = 0, updated: int = 0):
    conn.execute(
        "INSERT INTO notes(id, title, body, body_hash, indexed_at, deleted, updated_time) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (note_id, title, body, f"hash-{note_id}", deleted, updated),
    )


class TestScan:
    def test_finds_only_the_bad_ones(self, index) -> None:
        with db.transaction(index):
            _add(index, "n1", "", "Some body", updated=5)
            _add(index, "n2", "Untitled", "Other body", updated=4)
            _add(index, "n3", "A genuinely good title", "Body text", updated=3)
            _add(index, "n4", "20260114", "Journal entry", updated=2)

        found = {c.note_id: c.issue for c in find_candidates(index)}
        assert found == {"n1": "empty", "n2": "joplin_default", "n4": "date_only"}

    def test_deleted_notes_are_skipped(self, index) -> None:
        with db.transaction(index):
            _add(index, "n1", "Untitled", "body", deleted=1)
        assert find_candidates(index) == []

    def test_carries_context_for_review(self, index) -> None:
        with db.transaction(index):
            _add(index, "n1", "", "# OpenBao key rotation\n\nDetail", updated=7)
        [c] = find_candidates(index)
        assert c.body_hash == "hash-n1"
        assert c.updated_time == 7
        assert c.first_line == "OpenBao key rotation"  # gives generation a head start

    def test_limit_is_honoured(self, index) -> None:
        with db.transaction(index):
            for i in range(5):
                _add(index, f"n{i}", "Untitled", "body", updated=i)
        assert len(find_candidates(index, limit=2)) == 2

    def test_newest_first(self, index) -> None:
        with db.transaction(index):
            _add(index, "old", "Untitled", "b", updated=1)
            _add(index, "new", "Untitled", "b", updated=9)
        assert [c.note_id for c in find_candidates(index)] == ["new", "old"]

    def test_counts_per_rule(self, index) -> None:
        with db.transaction(index):
            _add(index, "n1", "", "b")
            _add(index, "n2", "Untitled", "b")
            _add(index, "n3", "New note", "b")
            _add(index, "n4", "Fine title here", "b")
        assert count_by_issue(index) == {"empty": 1, "joplin_default": 2}

    def test_long_body_does_not_break_the_head_read(self, index) -> None:
        """Only the body head is selected; a huge note must still classify."""
        body = "x" * 50_000
        with db.transaction(index):
            _add(index, "n1", "Untitled", body)
        [c] = find_candidates(index)
        assert c.issue == "joplin_default"


class TestGeneratability:
    """Flagged but untitleable: found by running the rules over a real vault.

    38% of candidates in a 2,240-note vault had no prose — bookmark notes whose
    body is the URL, and screenshots whose body is one resource embed. A model
    handed those invents something confident and wrong.
    """

    def test_screenshot_embed_has_no_prose(self) -> None:
        body = "![Screenshot_20250426-222044.png](:/fdcb7fa8db794095b3757bd84b401234)"
        assert prose_word_count(body) == 0

    def test_bare_bookmark_has_no_prose(self) -> None:
        assert prose_word_count("https://www.tecmint.com/install-fail2ban-to-protect-ssh/") == 0

    def test_prose_is_counted(self) -> None:
        body = "Spent the morning chasing why the accounting workers were double counting."
        assert prose_word_count(body) >= 10

    def test_prose_alongside_an_embed_still_counts(self) -> None:
        body = (
            "![shot.png](:/fdcb7fa8db794095b3757bd84b401234)\n\n"
            "This is the diagram William sent over for the reconciliation job design."
        )
        assert prose_word_count(body) >= 10

    def test_candidate_marks_untitleable_notes(self, index) -> None:
        with db.transaction(index):
            _add(index, "shot", "Screenshot_20250426.png", "![x](:/" + "a" * 32 + ")")
            _add(
                index,
                "prose",
                "",
                "Spent the morning chasing why the accounting workers double counted spend.",
            )
        found = {c.note_id: c.generatable for c in find_candidates(index)}
        assert found == {"shot": False, "prose": True}

    def test_still_flagged_even_when_not_generatable(self, index) -> None:
        """They are real defects; generation skips them, detection does not hide them."""
        with db.transaction(index):
            _add(index, "shot", "Screenshot_20250426.png", "![x](:/" + "a" * 32 + ")")
        [c] = find_candidates(index)
        assert c.issue == "filename"
        assert c.generatable is False
