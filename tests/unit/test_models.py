"""Tests for shared Pydantic models (PRD §13.2)."""

import pytest
from pydantic import ValidationError

from pkm_sidecar.models import ExtractedLink, NoteRow, NoteSummary


def test_note_summary_has_no_body_field() -> None:
    # Defence in depth: list views must never carry the full body.
    assert "body" not in NoteSummary.model_fields
    summary = NoteSummary(id="n1", title="T")
    assert "body" not in summary.model_dump()
    assert "body" not in summary.model_dump_json()


def test_note_row_carries_body() -> None:
    note = NoteRow(id="n1", title="T", body="content", body_hash="h", indexed_at=0)
    assert note.body == "content"


def test_extracted_link_type_is_constrained() -> None:
    ExtractedLink(note_id="n1", target=":/abc", link_type="internal_joplin")
    with pytest.raises(ValidationError):
        ExtractedLink(note_id="n1", target="x", link_type="not_a_valid_type")  # type: ignore[arg-type]
