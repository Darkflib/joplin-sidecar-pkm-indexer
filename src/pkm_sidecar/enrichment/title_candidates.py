"""Which notes need a better title (docs/enrichment.md §5).

Pure SQL and regex — no model, no network. Keeping detection separate from
generation matters because they fail differently: a mediocre generated title on a
genuinely untitled note is a small disappointment, while a confident suggestion
on a note whose title was already fine wastes review attention and erodes trust
in the whole feature. So these rules are tuned for **precision over recall**, and
each candidate carries the rule that selected it, so step 8 can measure them
individually against real accept/reject decisions and retire the ones that earn
their keep least.

Detection reads the index only. Filtering against existing suggestions and
opt-outs needs the enrichment store as well, so it belongs to the worker that
holds both connections, not here.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Literal

from pydantic import BaseModel

TitleIssue = Literal[
    "empty",
    "joplin_default",
    "date_only",
    "url",
    "filename",
    "truncated_from_body",
]

# Joplin's own placeholders, with an optional trailing counter ("Untitled 2").
_JOPLIN_DEFAULT_RE = re.compile(r"^(untitled|new note|new to-?do)(\s*\(?\d+\)?)?$", re.IGNORECASE)

# A title that is *only* a date or timestamp, in the formats a note-taking
# workflow actually produces. Anything with words in it ("Standup 2026-01-14")
# is left alone: it is weak, but flagging it costs precision.
_DATE_ONLY_RE = re.compile(
    r"""^(
        \d{8}                                   # 20260114
      | \d{6}                                   # 260114
      | \d{4}[-_/.]\d{1,2}[-_/.]\d{1,2}         # 2026-01-14
      | \d{1,2}[-_/.]\d{1,2}[-_/.]\d{2,4}       # 14/01/2026
    )
    (
        [ T]\d{1,2}[:.]\d{2}([:.]\d{2})?        # optional time
        (\s?[AaPp][Mm])?
    )?$""",
    re.VERBOSE,
)

_URL_RE = re.compile(r"^(https?://|www\.)\S*$", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(r"^[\w.-]+\.(com|org|net|io|dev|co\.uk|uk|edu|gov)(/\S*)?$", re.I)

_FILENAME_RE = re.compile(
    r"^\S.*\.(pdf|docx?|xlsx?|pptx?|txt|md|rtf|odt|ods|csv|eml|msg|html?|png|jpe?g)$",
    re.IGNORECASE,
)

# How much of a title must match the body's opening line before a strict prefix
# is read as Joplin truncating an auto-derived title rather than coincidence.
# A short title that happens to open the body ("RabbitMQ", body "RabbitMQ notes…")
# is far more likely deliberate, and flagging it would be a false positive.
MIN_TRUNCATION_LENGTH = 40

# Enough of the body to contain its first line in almost every case.
_BODY_HEAD_CHARS = 1000

_LEADING_MARKUP_RE = re.compile(r"^[#>\s*_-]+")


class TitleCandidate(BaseModel):
    """A note whose title looks like it needs replacing, and why."""

    note_id: str
    title: str
    issue: TitleIssue
    body_hash: str
    updated_time: int | None = None
    first_line: str = ""


def first_body_line(body: str) -> str:
    """The first line with content, stripped of leading Markdown decoration."""
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        return _LEADING_MARKUP_RE.sub("", line).strip()
    return ""


def classify_title(title: str, body_head: str = "") -> TitleIssue | None:
    """Return the rule that flags this title, or None if it looks fine.

    Rules are ordered most to least certain, and the first match wins, so a
    candidate's recorded reason is the strongest one that applied.
    """
    stripped = title.strip()
    if not stripped:
        return "empty"
    if _JOPLIN_DEFAULT_RE.match(stripped):
        return "joplin_default"
    if _DATE_ONLY_RE.match(stripped):
        return "date_only"
    if _URL_RE.match(stripped) or _BARE_DOMAIN_RE.match(stripped):
        return "url"
    if _FILENAME_RE.match(stripped):
        return "filename"

    line = first_body_line(body_head)
    # A strict prefix means the line carries on past the title — the signature of
    # Joplin cutting an auto-derived title short. An exact match is not a defect:
    # a note whose first line is a good heading has a good title.
    if (
        line
        and len(stripped) >= MIN_TRUNCATION_LENGTH
        and line.startswith(stripped)
        and len(line) > len(stripped)
    ):
        return "truncated_from_body"
    return None


def find_candidates(conn: sqlite3.Connection, *, limit: int | None = None) -> list[TitleCandidate]:
    """Scan the index for notes needing a better title.

    Only the head of each body is read: the rules need the opening line, not the
    whole note, and pulling full bodies for a 2,000-note vault would move
    megabytes to answer a regex.
    """
    sql = (
        "SELECT id, title, body_hash, updated_time, "
        f"substr(body, 1, {_BODY_HEAD_CHARS}) AS body_head "
        "FROM notes WHERE deleted = 0 ORDER BY updated_time DESC"
    )
    out: list[TitleCandidate] = []
    for row in conn.execute(sql):
        head = row["body_head"] or ""
        issue = classify_title(row["title"] or "", head)
        if issue is None:
            continue
        out.append(
            TitleCandidate(
                note_id=row["id"],
                title=row["title"] or "",
                issue=issue,
                body_hash=row["body_hash"],
                updated_time=row["updated_time"],
                first_line=first_body_line(head),
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def count_by_issue(conn: sqlite3.Connection) -> dict[str, int]:
    """Candidate counts per rule — the input to measuring precision per rule."""
    counts: dict[str, int] = {}
    for candidate in find_candidates(conn):
        counts[candidate.issue] = counts.get(candidate.issue, 0) + 1
    return counts
