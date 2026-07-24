"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [governance, portability]
status: active
--- /L9_META ---

TOML L9_META via a trailing `[tool.l9_meta]` table.

TOML tables cannot precede the document body without reparenting every
subsequent key, so the table is appended at the end of the file.
"""

from __future__ import annotations

import re

from tools.l9_meta.model import MetaRecord, fields_from_lines, record_from_fields

TABLE = "[tool.l9_meta]"

RE_TABLE = re.compile(r"\n*^\[tool\.l9_meta\][ \t]*$.*?(?=^\[|\Z)", re.MULTILINE | re.DOTALL)


def _quote_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


def render(rec: MetaRecord) -> str:
    return "\n".join(
        [
            TABLE,
            f"l9_schema = {rec.schema}",
            f'origin = "{rec.origin}"',
            f'engine = "{rec.engine}"',
            f"layer = {_quote_list(rec.layer)}",
            f"tags = {_quote_list(rec.tags)}",
            f'status = "{rec.status}"',
        ]
    )


def parse(text: str) -> MetaRecord | None:
    match = RE_TABLE.search(text)
    if not match:
        return None
    body = match.group(0).splitlines()[1:]
    normalized = [line.replace(" = ", ": ", 1).replace('"', "") for line in body]
    fields = fields_from_lines(normalized)
    if not fields:
        return None
    return record_from_fields(fields)


def strip(text: str) -> str:
    return RE_TABLE.sub("", text)


def inject(text: str, rec: MetaRecord) -> str:
    body = strip(text).rstrip("\n")
    return body + "\n\n" + render(rec) + "\n"
