"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [governance, portability]
status: active
--- /L9_META ---

L9_META record model — schema v2 (owner dropped, see contracts/contract_18.yaml).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

SCHEMA_VERSION = 2
SUPPORTED_SCHEMAS = (1, 2)

# Emission order. `owner` is parsed for v1 compatibility but never re-emitted.
FIELD_ORDER = ("l9_schema", "origin", "engine", "layer", "tags", "status")

_FIELD_NAMES = frozenset(FIELD_ORDER) | {"owner"}


class MetaError(ValueError):
    """Raised when a header cannot be parsed or a record is invalid."""


@dataclass(frozen=True)
class MetaRecord:
    """One file's L9_META values."""

    origin: str
    engine: str
    layer: tuple[str, ...]
    tags: tuple[str, ...]
    status: str = "active"
    schema: int = SCHEMA_VERSION

    def normalized(self) -> MetaRecord:
        """Return a copy with deterministic list ordering and v2 schema.

        Ordering is positional, not sorted: facet position carries meaning
        (family, capability, concern). Only duplicates are removed.
        """
        return replace(
            self,
            layer=_dedupe(self.layer),
            tags=_dedupe(self.tags),
            schema=SCHEMA_VERSION,
        )

    def as_dict(self) -> dict[str, Any]:
        """Field mapping in emission order."""
        return {
            "l9_schema": self.schema,
            "origin": self.origin,
            "engine": self.engine,
            "layer": list(self.layer),
            "tags": list(self.tags),
            "status": self.status,
        }


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item.strip(), None)
    return tuple(k for k in seen if k)


def as_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a config value (list, comma string, or scalar) to a tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        return parse_scalar_list(value)
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    msg = f"expected list or string, got {type(value).__name__}"
    raise MetaError(msg)


def parse_scalar_list(raw: str) -> tuple[str, ...]:
    """Parse a YAML flow sequence (`[a, b]`) or bare scalar into a tuple."""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return tuple(part.strip().strip("'\"") for part in text.split(",") if part.strip())


def render_scalar_list(items: tuple[str, ...] | list[str]) -> str:
    """Render a tuple as a YAML flow sequence."""
    return "[" + ", ".join(items) + "]"


def field_lines(rec: MetaRecord) -> list[str]:
    """Render the field body shared by every text format (no delimiters)."""
    return [
        f"l9_schema: {rec.schema}",
        f"origin: {rec.origin}",
        f"engine: {rec.engine}",
        f"layer: {render_scalar_list(rec.layer)}",
        f"tags: {render_scalar_list(rec.tags)}",
        f"status: {rec.status}",
    ]


_FIELD_RE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<value>.*)$")


def record_from_fields(fields: dict[str, str]) -> MetaRecord:
    """Build a record from a parsed key/value mapping.

    Accepts both v1 (with `owner`) and v2 headers. Unknown keys are rejected so
    a malformed header fails loudly instead of silently losing data.
    """
    unknown = set(fields) - _FIELD_NAMES
    if unknown:
        msg = f"unknown L9_META field(s): {sorted(unknown)}"
        raise MetaError(msg)

    missing = {"origin", "engine"} - set(fields)
    if missing:
        msg = f"missing required L9_META field(s): {sorted(missing)}"
        raise MetaError(msg)

    raw_schema = fields.get("l9_schema", str(SCHEMA_VERSION)).strip()
    try:
        schema = int(raw_schema)
    except ValueError as exc:
        msg = f"l9_schema is not an integer: {raw_schema!r}"
        raise MetaError(msg) from exc
    if schema not in SUPPORTED_SCHEMAS:
        msg = f"unsupported l9_schema: {schema} (supported: {list(SUPPORTED_SCHEMAS)})"
        raise MetaError(msg)

    return MetaRecord(
        origin=fields["origin"].strip(),
        engine=fields["engine"].strip(),
        layer=parse_scalar_list(fields.get("layer", "")),
        tags=parse_scalar_list(fields.get("tags", "")),
        status=fields.get("status", "active").strip() or "active",
        schema=schema,
    )


def fields_from_lines(lines: list[str]) -> dict[str, str]:
    """Extract `key: value` pairs from header body lines, ignoring blanks."""
    fields: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = _FIELD_RE.match(stripped)
        if match:
            fields[match.group("key")] = match.group("value")
    return fields
