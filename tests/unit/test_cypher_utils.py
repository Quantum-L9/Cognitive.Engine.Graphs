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
@pytest.mark.parametrize("name", ["plasticos", "healthcare-referral", "acme.tenant-01", "abc", "a" * 63])
def test_sanitize_database_name_accepts_neo4j_database_names(name: str) -> None:
    from engine.utils.security import sanitize_database_name, validate_tenant_id

    assert sanitize_database_name(name) == name
    # One identifier, one grammar: tenant id == database name (CEG-DBNAME-01).
    assert validate_tenant_id(name) == name


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "plasticos`; DROP DATABASE neo4j; --",  # back-quote escape
        "1leading-digit",
        "ab",  # under the 3-character minimum
        "has space",
        "",
        "a" * 64,  # over the 63-character maximum
        "Plasticos",  # Neo4j would lowercase it; the contract refuses instead of normalising
        "acme.tenant_01",  # underscore: legal for Neo4j, refused by the tenant grammar
        "system-of-record",  # reserved prefix
        "systemx",  # reserved prefix with no separator
    ],
)
def test_sanitize_database_name_rejects_unsafe_names(name: str) -> None:
    from engine.utils.security import sanitize_database_name, validate_tenant_id

    with pytest.raises(ValueError, match="refusing to provision"):
        sanitize_database_name(name)
    with pytest.raises(ValueError):
        validate_tenant_id(name)
