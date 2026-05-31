"""Line-oriented Markdown task and link extraction (PRD §12).

Deliberately simple — no Markdown AST. We scan line by line, skipping fenced code
blocks (``` and ~~~), and recognise:

* **Tasks**: ``- [ ]`` / ``- [x]`` / ``- [X]`` with ``-``, ``*`` or ``+`` bullets,
  at any indent. ``x`` and ``X`` both count as checked.
* **Links**: inline ``[text](target)`` and images ``![alt](src)``. Inline code
  spans are stripped first so links inside backticks are ignored. Targets are
  classified as ``internal_joplin`` (``:/`` + 32 hex), ``external_url`` (has a
  ``scheme://``), ``other`` (a scheme without ``//``, e.g. ``mailto:``/``tel:``),
  or ``relative``.

Explicitly **not** handled in v0.1 (documented limitation): reference-style links
(``[text][ref]``), wiki/``[[double-bracket]]`` links, and HTML anchors. A task
line that also contains a link yields both a task row and a link row.
"""

from __future__ import annotations

import re
from typing import TypedDict

from pkm_sidecar.models import ExtractedLink, ExtractedTask, LinkType


class ExtractResult(TypedDict):
    tasks: list[ExtractedTask]
    links: list[ExtractedLink]


# Bullet task: optional indent, -/*/+ bullet, [ ]/[x]/[X], then text.
_TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s?(.*)$")
# A line that opens or closes a fenced code block.
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
# Inline link or image: optional leading '!', [text](target).
_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")
# Inline code span, removed before link scanning.
_CODE_SPAN_RE = re.compile(r"`[^`]*`")

_INTERNAL_RE = re.compile(r"^:/[0-9a-fA-F]{32}$")
_SCHEME_SLASHES_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def classify_link(target: str) -> LinkType:
    """Classify a link target (PRD §12.2)."""
    if _INTERNAL_RE.match(target):
        return "internal_joplin"
    if _SCHEME_SLASHES_RE.match(target):
        return "external_url"
    if _SCHEME_RE.match(target):
        # A scheme without '//', e.g. mailto: / tel:.
        return "other"
    return "relative"


def extract(note_id: str, body: str) -> ExtractResult:
    """Extract tasks and links from *body*; returns ``{"tasks": [...], "links": [...]}``."""
    tasks: list[ExtractedTask] = []
    links: list[ExtractedLink] = []
    in_fence = False

    for index, raw in enumerate(body.split("\n"), start=1):
        line = raw.rstrip("\r")

        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        task_match = _TASK_RE.match(line)
        if task_match:
            tasks.append(
                ExtractedTask(
                    note_id=note_id,
                    line_number=index,
                    checked=task_match.group(1) in ("x", "X"),
                    text=task_match.group(2).strip(),
                    raw_line=line,
                )
            )

        without_code = _CODE_SPAN_RE.sub("", line)
        for match in _LINK_RE.finditer(without_code):
            is_image = match.group(1) == "!"
            text = match.group(2)
            target = match.group(3).strip()
            links.append(
                ExtractedLink(
                    note_id=note_id,
                    line_number=index,
                    link_text=("!" + text) if is_image else text,
                    target=target,
                    link_type=classify_link(target),
                )
            )

    return {"tasks": tasks, "links": links}
