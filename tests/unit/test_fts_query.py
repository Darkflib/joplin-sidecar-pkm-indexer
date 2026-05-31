"""Tests for FTS query escaping (PRD §19.1 'FTS query escaping/safety')."""

from pkm_sidecar.repositories import build_fts_match_expression


def test_single_term() -> None:
    assert build_fts_match_expression("rabbitmq") == '"rabbitmq"'


def test_multiple_terms_implicit_and() -> None:
    assert build_fts_match_expression("rabbit mq guide") == '"rabbit" "mq" "guide"'


def test_embedded_quote_doubled() -> None:
    assert build_fts_match_expression('say "hi"') == '"say" """hi"""'


def test_empty_query() -> None:
    assert build_fts_match_expression("") == ""
    assert build_fts_match_expression("   ") == ""


def test_operators_become_literal_phrases() -> None:
    # FTS5 operators must not pass through as syntax — they become quoted literals.
    assert build_fts_match_expression("a AND b") == '"a" "AND" "b"'
    assert build_fts_match_expression("a OR b*") == '"a" "OR" "b*"'
