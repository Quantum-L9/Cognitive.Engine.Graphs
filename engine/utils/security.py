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
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", label):
        msg = f"Invalid label or type: {label!r}"
        raise ValueError(msg)
    return label


# Neo4j database naming rules: begins with an ASCII letter, then letters,
# digits, dots, dashes or underscores, 3-63 characters. Domain ids legitimately
# contain dashes ("healthcare-referral"), which is why sanitize_label does not
# apply here: its label grammar forbids them.
_DATABASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,62}$")


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
    if not _DATABASE_NAME_RE.fullmatch(name):
        msg = (
            f"refusing to provision Neo4j database {name!r}: a database name must begin "
            f"with a letter and contain only letters, digits, dots, dashes or underscores "
            f"(3-63 characters). CREATE DATABASE takes no query parameter, so an "
            f"unvalidated name would be interpolated into an administrative command."
        )
        raise ValueError(msg)
    return name
