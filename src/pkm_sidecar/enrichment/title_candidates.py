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

from pydantic import BaseModel, computed_field

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

# A single whitespace-free token. `\S.*` would swallow "Read the attached
# report.pdf" and "Notes about screenshot.png" — prose titles that merely end in
# an extension — which is the false-positive class this detector exists to avoid.
# The trade is that filenames containing spaces are missed; in a real 2,240-note
# vault every hit was of the form Screenshot_20250426-222044.png, so the stricter
# rule loses nothing and removes a whole category of wrong suggestion.
_FILENAME_RE = re.compile(
    r"^\S+\.(pdf|docx?|xlsx?|pptx?|txt|md|rtf|odt|ods|csv|eml|msg|html?|png|jpe?g)$",
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

# A note body can be flagged as badly titled and still be impossible to title.
# Measured against a real 2,240-note vault, 38% of candidates had no prose at
# all: bookmark notes whose body *is* the URL, and screenshots whose body is a
# single `![name](:/id)` embed. Resource indexing is metadata-only and OCR is out
# of scope, so a model handed those would invent something confident and wrong —
# the precise failure that erodes trust in a review queue. Detection still flags
# them (they are real defects); generation skips them.
_RESOURCE_EMBED_RE = re.compile(r"!?\[[^\]]*\]\(:/[0-9a-fA-F]{32}\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_MARKUP_CHARS_RE = re.compile(r"[#*_>`\[\]()|~-]+")

MIN_PROSE_WORDS = 10


class TitleCandidate(BaseModel):
    """A note whose title looks like it needs replacing, and why."""

    note_id: str
    title: str
    issue: TitleIssue
    body_hash: str
    updated_time: int | None = None
    first_line: str = ""
    prose_words: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def generatable(self) -> bool:
        """Whether there is enough text for a model to title this from."""
        return self.prose_words >= MIN_PROSE_WORDS


def first_body_line(body: str) -> str:
    """The first line with content, stripped of leading Markdown decoration."""
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        cleaned = _LEADING_MARKUP_RE.sub("", line).strip()
        # A rule ("---"), a frontmatter fence or a bare blockquote marker strips
        # to nothing. Returning here would blank first_line and silently disable
        # truncation detection for every note that opens that way.
        if cleaned:
            return cleaned
    return ""


def prose_word_count(text: str) -> int:
    """Words left after resource embeds, bare URLs and Markdown are removed.

    This is the signal for whether a note has anything to title *from*. A
    screenshot note's body is one embed; a bookmark's is one URL. Both strip to
    nothing.
    """
    stripped = _RESOURCE_EMBED_RE.sub(" ", text)
    stripped = _BARE_URL_RE.sub(" ", stripped)
    stripped = _MARKUP_CHARS_RE.sub(" ", stripped)
    return sum(1 for word in stripped.split() if any(ch.isalpha() for ch in word))


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
    if limit is not None and limit <= 0:
        return []
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
                prose_words=prose_word_count(head),
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
