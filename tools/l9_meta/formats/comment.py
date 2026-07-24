"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [governance, portability]
status: active
--- /L9_META ---

Comment-style L9_META blocks: YAML, shell, Makefile, Dockerfile, CODEOWNERS.

Canonical form:

    # --- L9_META ---
    # l9_schema: 2
    # ...
    # --- /L9_META ---

Detection is delimiter-anchored: a line must be exactly the open or close
delimiter. Prose that merely mentions L9_META is never treated as a header.
"""

from __future__ import annotations

import re

from tools.l9_meta.model import (
    MetaRecord,
    field_lines,
    fields_from_lines,
    record_from_fields,
)

PREFIX = "#"
OPEN = "# --- L9_META ---"
CLOSE = "# --- /L9_META ---"

# Anchored at column 0: the canonical block is always emitted flush-left, so an
# indented delimiter is documentation showing the format, not a header. Allowing
# leading whitespace made this module's own docstring parse as a malformed header.
RE_OPEN = re.compile(r"^#[ \t]*---[ \t]*L9_META[ \t]*---[ \t]*$")
RE_CLOSE = re.compile(r"^#[ \t]*---[ \t]*/L9_META[ \t]*---[ \t]*$")

# Legacy bare form: `# L9_META` with no `---` delimiters, optionally wrapped in
# `# =====` banner lines and sometimes lacking a `# /L9_META` close sentinel.
RE_LEGACY_OPEN = re.compile(r"^[ \t]*#[ \t]*L9_META[ \t]*$")
RE_LEGACY_CLOSE = re.compile(r"^[ \t]*#[ \t]*/L9_META[ \t]*$")
RE_BANNER = re.compile(r"^[ \t]*#[ \t]*[=\-*_]{4,}[ \t]*$")
RE_COMMENT_FIELD = re.compile(r"^[ \t]*#[ \t]*[a-z_][a-z0-9_]*[ \t]*:")
RE_BARE_COMMENT = re.compile(r"^[ \t]*#[ \t]*$")

# Uncommented block accidentally written into a YAML/text file.
RE_UNCOMMENTED = re.compile(
    r"^---[ \t]*L9_META[ \t]*---.*?^---[ \t]*/L9_META[ \t]*---[ \t]*\n?",
    re.MULTILINE | re.DOTALL,
)


def render(rec: MetaRecord) -> str:
    body = [f"{PREFIX} {line}" for line in field_lines(rec)]
    return "\n".join([OPEN, *body, CLOSE])


def _uncomment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith(PREFIX):
        return stripped[len(PREFIX) :].lstrip()
    return stripped


def _head_limit(lines: list[str]) -> int:
    """Index of the first line that is neither a comment, a shebang, nor blank.

    A header lives in the file's leading comment region. Without this bound the
    finders scan the whole file, so a `# --- L9_META ---` inside a test fixture
    string or a fenced doc example is read as that file's header — which then
    reports as malformed because the surrounding prose has no field lines.
    """
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return i
    return len(lines)


def _find_canonical(lines: list[str]) -> tuple[int, int] | None:
    """Return (start, end_exclusive) of the first canonical block."""
    limit = _head_limit(lines)
    for i, line in enumerate(lines[:limit]):
        if RE_OPEN.match(line):
            for j in range(i + 1, len(lines)):
                if RE_CLOSE.match(lines[j]):
                    return i, j + 1
            return None
    return None


def _find_legacy(lines: list[str]) -> tuple[int, int] | None:
    """Return (start, end_exclusive) of the first legacy bare-form block.

    The block runs from the `# L9_META` opener (plus an immediately preceding
    banner line, if any) through the last consecutive banner, field, or close
    sentinel line. Scanning stops at the first line that is none of those, so a
    block without a close sentinel still terminates correctly.
    """
    limit = _head_limit(lines)
    for i, line in enumerate(lines[:limit]):
        if not RE_LEGACY_OPEN.match(line):
            continue
        start = i - 1 if i > 0 and RE_BANNER.match(lines[i - 1]) else i
        end = i + 1
        for j in range(i + 1, len(lines)):
            candidate = lines[j]
            if RE_LEGACY_CLOSE.match(candidate):
                end = j + 1
                break
            if RE_BANNER.match(candidate) or RE_COMMENT_FIELD.match(candidate) or RE_BARE_COMMENT.match(candidate):
                end = j + 1
                continue
            break
        return start, end
    return None


def parse(text: str) -> MetaRecord | None:
    lines = text.splitlines()
    span = _find_canonical(lines)
    if span is not None:
        start, end = span
        body = [_uncomment(line) for line in lines[start + 1 : end - 1]]
        return record_from_fields(fields_from_lines(body))

    span = _find_legacy(lines)
    if span is not None:
        start, end = span
        body = [_uncomment(line) for line in lines[start:end] if RE_COMMENT_FIELD.match(line)]
        fields = fields_from_lines(body)
        if fields:
            return record_from_fields(fields)
    return None


def strip(text: str) -> str:
    """Remove every canonical, legacy, and uncommented L9_META block."""
    text = RE_UNCOMMENTED.sub("", text)
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()

    for finder in (_find_canonical, _find_legacy):
        while True:
            span = finder(lines)
            if span is None:
                break
            start, end = span
            del lines[start:end]

    result = "\n".join(lines)
    if trailing_newline and result:
        result += "\n"
    return result


def inject(text: str, rec: MetaRecord) -> str:
    body = strip(text)
    block = render(rec)

    if body.startswith("#!"):
        newline = body.find("\n")
        if newline == -1:
            return f"{body}\n{block}\n"
        return body[: newline + 1] + block + "\n" + body[newline + 1 :]

    return block + "\n" + body.lstrip("\n")
