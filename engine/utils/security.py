"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [utils, security, sanitize]
owner: engine-team
status: active
--- /L9_META ---

engine/utils/security.py
Security utilities for the L9 Graph Cognitive Engine.
"""

from __future__ import annotations

import re

_MAX_LABEL_LEN = 64


def sanitize_label(label: str) -> str:
    """
    Validate Neo4j label/relationship type to prevent Cypher injection.

    SECURITY: Labels are interpolated into Cypher queries. User-uploaded
    domain specs could contain malicious labels with injection payloads.

    Valid labels: [A-Za-z_][A-Za-z0-9_]*, max 64 characters.

    Raises ValueError if invalid.
    """
    if len(label) > _MAX_LABEL_LEN:
        msg = f"Label exceeds maximum length of {_MAX_LABEL_LEN}: {label!r}"
        raise ValueError(msg)
    # RU-077 / CEG-COR-126: fullmatch — `$` also matches before a trailing newline.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
        msg = f"Invalid label or type: {label!r}"
        raise ValueError(msg)
    return label


def cypher_number(value: object) -> float:
    """
    Validate a domain-spec scalar before it is interpolated into Cypher as a numeric literal.

    SECURITY: a number cannot carry Cypher, so a value that survives ``float()``
    is safe to interpolate; anything else (a string payload, ``None`` where the
    spec promised a number) is rejected here instead of reaching the query.

    Raises ValueError if the value is not numeric.
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        msg = f"Expected a numeric Cypher literal, got {value!r}"
        raise ValueError(msg) from exc


# Tenant identity contract (CEG-IDENT-01 / CEG-DBNAME-01, decided 2026-10-22):
# tenant id == domain spec id == spec directory == Neo4j database name, with no
# mapping layer between them. The grammar is therefore the Neo4j 5 database-name
# grammar, lowercase only: begins with an ASCII letter, then lowercase letters,
# digits, dots or dashes, 3-63 characters. Neo4j normalises database names to
# lowercase and accepts underscores; this contract refuses both rather than
# normalising, so the id a packet carries, the directory the loader opens and
# the database the driver binds are byte-identical on every node. Domain ids
# legitimately contain dashes ("healthcare-referral"), which is why
# sanitize_label does not apply here: its label grammar forbids them.
_TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9.-]{2,62}$")
# Reserved by Neo4j: no user database may begin with this prefix.
_RESERVED_DATABASE_PREFIX = "system"
TENANT_ID_GRAMMAR = (
    "lowercase, 3-63 characters, starts with a letter, then only letters, digits, "
    "dots or dashes, and never starting with 'system'"
)


def tenant_id_defect(name: str) -> str | None:
    """Return why ``name`` is not a legal tenant id, or None when it is legal."""
    if not _TENANT_ID_RE.fullmatch(name):
        return f"{name!r} is not a legal tenant id ({TENANT_ID_GRAMMAR})"
    if name.startswith(_RESERVED_DATABASE_PREFIX):
        return f"{name!r} starts with the Neo4j-reserved prefix {_RESERVED_DATABASE_PREFIX!r}"
    return None


def validate_tenant_id(name: str) -> str:
    """Validate a tenant id against the identity contract; return it unchanged.

    The tenant id is the spec id and the database name, so this is the single
    grammar every identity surface applies: the loader before it touches the
    filesystem, the driver before it provisions a database. Fails closed: an
    illegal id is refused, never lowercased or otherwise repaired.

    Raises ValueError if invalid.
    """
    defect = tenant_id_defect(name)
    if defect is not None:
        raise ValueError(defect)
    return name


def sanitize_database_name(name: str) -> str:
    """
    Validate a Neo4j database name before it is quoted into an administrative command.

    SECURITY: ``CREATE DATABASE`` cannot take the name as a query parameter (it is
    an administrative command, not a read/write query), so the name is back-quoted
    into the statement — and therefore must be validated first. The grammar above
    is what makes that quoting safe: no back-quote, whitespace or statement
    separator can pass it.

    Raises ValueError if invalid.
    """
    defect = tenant_id_defect(name)
    if defect is not None:
        msg = (
            f"refusing to provision Neo4j database {name!r}: {defect}. CREATE DATABASE "
            f"takes no query parameter, so an unvalidated name would be interpolated "
            f"into an administrative command."
        )
        raise ValueError(msg)
    return name
