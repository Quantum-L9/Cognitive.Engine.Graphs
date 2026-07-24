"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [governance, portability]
status: active
--- /L9_META ---

JSON L9_META via a leading `_l9_meta` object key.

Arrays cannot carry the key without changing their shape, so array documents are
left untouched and report as unsupported.
"""

from __future__ import annotations

import json

from tools.l9_meta.model import MetaRecord, record_from_fields

KEY = "_l9_meta"


class NotInjectableError(Exception):
    """Raised when a JSON document cannot carry a header (e.g. a top-level array)."""


def render(rec: MetaRecord) -> dict[str, object]:
    return rec.as_dict()


def _load(text: str) -> object:
    return json.loads(text)


def parse(text: str) -> MetaRecord | None:
    try:
        data = _load(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get(KEY)
    if not isinstance(raw, dict):
        return None
    fields = {k: ("[" + ", ".join(map(str, v)) + "]" if isinstance(v, list) else str(v)) for k, v in raw.items()}
    return record_from_fields(fields)


def strip(text: str) -> str:
    try:
        data = _load(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict) or KEY not in data:
        return text
    data.pop(KEY)
    return json.dumps(data, indent=2) + "\n"


def inject(text: str, rec: MetaRecord) -> str:
    try:
        data = _load(text)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON: {exc}"
        raise NotInjectableError(msg) from exc
    if not isinstance(data, dict):
        msg = "top-level JSON array cannot carry an L9_META key"
        raise NotInjectableError(msg)
    data.pop(KEY, None)
    ordered: dict[str, object] = {KEY: render(rec)}
    ordered.update(data)
    return json.dumps(ordered, indent=2) + "\n"
