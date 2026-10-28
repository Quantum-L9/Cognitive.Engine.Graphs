"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api, compliance]
tags: [chassis, pii, redaction]
owner: platform-team
status: active
--- /L9_META ---

chassis/pii.py — Engine-Agnostic PII Detection & Masking

Extracted from engine/compliance/pii.py. Zero engine imports.

Every constellation node that touches customer data needs PII handling:
    - ENRICH sees raw CRM fields (email, phone, addresses)
    - GRAPH stores entity properties that may contain PII
    - SCORE/ROUTE/FORECAST pass through PII in payloads

The chassis provides detection + masking. Engine-specific GDPR erasure
logic stays in the engine (it needs driver access).
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class PIICategory(StrEnum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    ADDRESS = "address"
    DOB = "date_of_birth"
    FINANCIAL = "financial"
    IP_ADDRESS = "ip_address"
    CUSTOM = "custom"


class PIISensitivity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_PII_PATTERNS: dict[PIICategory, re.Pattern] = {
    PIICategory.EMAIL: re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    PIICategory.PHONE: re.compile(r"(?:\+?1[\-\s.]?)?\(?\d{3}\)?[\-\s.]?\d{3}[\-\s.]?\d{4}"),
    PIICategory.SSN: re.compile(r"\b\d{3}[\-\s]?\d{2}[\-\s]?\d{4}\b"),
    PIICategory.IP_ADDRESS: re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

_PII_FIELD_HINTS: dict[str, tuple[PIICategory, PIISensitivity]] = {
    "email": (PIICategory.EMAIL, PIISensitivity.MEDIUM),
    "phone": (PIICategory.PHONE, PIISensitivity.MEDIUM),
    "ssn": (PIICategory.SSN, PIISensitivity.HIGH),
    "social_security": (PIICategory.SSN, PIISensitivity.HIGH),
    "date_of_birth": (PIICategory.DOB, PIISensitivity.HIGH),
    "dob": (PIICategory.DOB, PIISensitivity.HIGH),
    "first_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "last_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "full_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "address": (PIICategory.ADDRESS, PIISensitivity.MEDIUM),
    "account_number": (PIICategory.FINANCIAL, PIISensitivity.HIGH),
    "credit_score": (PIICategory.FINANCIAL, PIISensitivity.HIGH),
    "ip_address": (PIICategory.IP_ADDRESS, PIISensitivity.LOW),
}


class PIIDetection(BaseModel):
    field_path: str
    category: PIICategory
    sensitivity: PIISensitivity
    detected_by: str


class PIIHandler:
    """
    Detect, mask, and redact PII in arbitrary payloads.
    Zero engine dependencies — works with any dict[str, Any].
    """

    def __init__(
        self,
        additional_fields: dict[str, tuple[PIICategory, PIISensitivity]] | None = None,
        mask_char: str = "*",
    ):
        self._hints = dict(_PII_FIELD_HINTS)
        if additional_fields:
            self._hints.update(additional_fields)
        self._mask_char = mask_char

    def _detect_by_key(self, key: str, path: str) -> PIIDetection | None:
        """Check if a field key matches a known PII hint."""
        key_lower = key.lower()
        for hint, (cat, sens) in self._hints.items():
            if hint in key_lower:
                return PIIDetection(field_path=path, category=cat, sensitivity=sens, detected_by="field_name")
        return None

    def _detect_by_pattern(self, value: str, path: str) -> PIIDetection | None:
        """Check if a string value matches a PII regex pattern."""
        for cat, pattern in _PII_PATTERNS.items():
            if pattern.search(value):
                sens = self._hints.get(cat.value, (cat, PIISensitivity.MEDIUM))[1]
                return PIIDetection(field_path=path, category=cat, sensitivity=sens, detected_by="pattern")
        return None

    def detect(self, payload: dict[str, Any] | list[Any], prefix: str = "") -> list[PIIDetection]:
        """Recursively detect PII fields in a payload (dicts and lists/tuples, any depth).

        A key matching a PII hint is flagged whatever its value type (a
        dict-valued ``address`` included). Paths are unambiguous: ``a.b[0].c``,
        with ``.``, ``[``, ``]``, ``"`` and ``\\`` inside a key escaped by a
        backslash (``user\\.email``) and the empty key written ``""``, so every
        detected path is addressable by ``mask`` / ``redact``. Simple
        nested-dict paths are unchanged.
        """
        base = _parse_path(prefix) if prefix else ()
        return [d for _, d in self._detect(payload, base)]

    def _detect(self, node: Any, path: tuple[_Part, ...]) -> list[tuple[tuple[_Part, ...], PIIDetection]]:
        found: list[tuple[tuple[_Part, ...], PIIDetection]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                key_path = (*path, str(key))
                hit = self._detect_by_key(str(key), _format_path(key_path))
                if hit is not None:
                    found.append((key_path, hit))
                elif isinstance(value, (dict, list, tuple)):
                    found.extend(self._detect(value, key_path))
                elif isinstance(value, str):
                    hit = self._detect_by_pattern(value, _format_path(key_path))
                    if hit is not None:
                        found.append((key_path, hit))
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                item_path = (*path, index)
                if isinstance(item, (dict, list, tuple)):
                    found.extend(self._detect(item, item_path))
                elif isinstance(item, str):
                    hit = self._detect_by_pattern(item, _format_path(item_path))
                    if hit is not None:
                        found.append((item_path, hit))
        return found

    def get_pii_paths(self, payload: dict[str, Any] | list[Any]) -> tuple[str, ...]:
        """For TransportPacket.security.pii_fields."""
        return tuple(d.field_path for d in self.detect(payload))

    def _paths(self, payload: Any, fields: list[str] | None) -> list[tuple[_Part, ...]]:
        if fields is None:
            return [path for path, _ in self._detect(payload, ())]
        return [_parse_path(f) for f in fields]

    def mask(self, payload: Any, fields: list[str] | None = None) -> Any:
        """Mask specified (or all detected) PII fields. Returns a new structure; the input is not mutated."""
        result = _shallow(payload)
        for path in self._paths(payload, fields):
            if path:
                result = _edit(result, path, lambda v: self._mask_char * min(len(str(v)), 12))
        return result

    def redact(self, payload: Any, fields: list[str] | None = None) -> Any:
        """Remove PII fields entirely. Returns a new structure; the input is not mutated.

        Deepest / highest-index paths are removed first so deleting a list
        element never shifts another path still to be applied.
        """
        result = _shallow(payload)
        paths = {p for p in self._paths(payload, fields) if p}
        for path in sorted(paths, key=_sort_key, reverse=True):
            result = _edit(result, path, lambda _v: _DELETE)
        return result

    @staticmethod
    def hash_value(value: str, salt: str = "") -> str:
        """SHA-256 pseudonymization."""
        return hashlib.sha256(f"{salt}{value}".encode()).hexdigest()


# ── Structured, escaped paths (RU-071) ────────────────────

_Part = str | int
_DELETE = object()
_ESCAPE_RE = re.compile(r'([.\[\]\\"])')
# The empty key has no characters to escape; it is written as an unescaped
# `""` (a literal `"` in a key is always escaped, so this cannot collide).
_EMPTY_KEY = '""'


def _format_path(path: tuple[_Part, ...]) -> str:
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            escaped = _ESCAPE_RE.sub(r"\\\1", part) if part else _EMPTY_KEY
            out += f".{escaped}" if out else escaped
    return out


def _malformed(path: str) -> ValueError:
    msg = f"Malformed PII field path {path!r}: empty key component (write the empty key as {_EMPTY_KEY})"
    return ValueError(msg)


def _parse_path(path: str) -> tuple[_Part, ...]:
    parts: list[_Part] = []
    key: list[str] = []
    have_key = False
    escaped = False
    after_dot = False

    def close_key() -> None:
        raw = "".join(key)
        parts.append("" if raw == _EMPTY_KEY and not escaped else raw)

    i = 0
    while i < len(path):
        ch = path[i]
        if ch == "\\" and i + 1 < len(path):
            key.append(path[i + 1])
            have_key = escaped = True
            after_dot = False
            i += 2
            continue
        if ch in ".[":
            if have_key:
                close_key()
            elif after_dot or (ch == "." and not parts):
                raise _malformed(path)
            key, have_key, escaped = [], False, False
            after_dot = ch == "."
            if ch == "[":
                end = path.index("]", i)
                parts.append(int(path[i + 1 : end]))
                i = end
        else:
            key.append(ch)
            have_key = True
            after_dot = False
        i += 1
    if have_key:
        close_key()
    elif after_dot:
        raise _malformed(path)
    return tuple(parts)


def _sort_key(path: tuple[_Part, ...]) -> tuple[tuple[int, Any], ...]:
    return tuple((0, p) if isinstance(p, int) else (1, p) for p in path)


def _shallow(payload: Any) -> Any:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, list):
        return list(payload)
    return payload


def _edit(node: Any, path: tuple[_Part, ...], transform: Any) -> Any:
    """Return ``node`` with ``transform`` applied at ``path``, copying containers on the path."""
    if not path:
        return transform(node)
    head, rest = path[0], path[1:]
    if isinstance(node, dict) and isinstance(head, str):
        if head not in node:
            return node
        new_value = _edit(node[head], rest, transform)
        copied = dict(node)
        if new_value is _DELETE:
            copied.pop(head)
        else:
            copied[head] = new_value
        return copied
    if isinstance(node, (list, tuple)) and isinstance(head, int):
        if head >= len(node):
            return node
        new_value = _edit(node[head], rest, transform)
        items = list(node)
        if new_value is _DELETE:
            del items[head]
        else:
            items[head] = new_value
        return tuple(items) if isinstance(node, tuple) else items
    return node
