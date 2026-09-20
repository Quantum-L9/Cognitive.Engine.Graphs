"""Unit tests — Cypher utility functions."""

from __future__ import annotations

import pytest


def test_sanitize_label_accepts_valid():
    from engine.utils.security import sanitize_label

    assert sanitize_label("Facility") == "Facility"
    assert sanitize_label("PolymerFamily") == "PolymerFamily"


def test_sanitize_label_rejects_spaces():
    from engine.utils.security import sanitize_label

    with pytest.raises((ValueError, Exception)):
        sanitize_label("bad label")


def test_sanitize_label_rejects_empty():
    from engine.utils.security import sanitize_label

    with pytest.raises((ValueError, Exception)):
        sanitize_label("")


def test_sanitize_label_rejects_sql_injection():
    from engine.utils.security import sanitize_label

    with pytest.raises((ValueError, Exception)):
        sanitize_label("'; DROP TABLE")


def test_sanitize_label_rejects_numeric_start():
    from engine.utils.security import sanitize_label

    with pytest.raises((ValueError, Exception)):
        sanitize_label("123Label")


def test_sanitize_label_rejects_too_long():
    from engine.utils.security import sanitize_label

    with pytest.raises((ValueError, Exception)):
        sanitize_label("A" * 200)


@pytest.mark.unit
def test_cypher_quoted_ident_wraps_sanitized_label():
    from engine.utils.security import cypher_quoted_ident

    assert cypher_quoted_ident("Facility") == "'Facility'"
    assert cypher_quoted_ident("closed_won") == "'closed_won'"


@pytest.mark.unit
def test_cypher_quoted_ident_rejects_injection():
    from engine.utils.security import cypher_quoted_ident

    with pytest.raises((ValueError, Exception)):
        cypher_quoted_ident("'; DROP TABLE")
