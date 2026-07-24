"""HTML-comment L9_META blocks for Markdown files.

Canonical form:

    <!-- L9_META
    l9_schema: 2
    ...
    /L9_META -->

Front matter is load-bearing in this repo: `.claude/agents/*.md`,
`.claude/rules/*.md`, and `.claude/skills/*/SKILL.md` are discovered by parsing
YAML front matter that must remain at byte 0. When front matter is present the
header is inserted immediately after it, never before.
"""

from __future__ import annotations

import re

from tools.l9_meta.model import (
    MetaRecord,
    field_lines,
    fields_from_lines,
    record_from_fields,
)

OPEN = "<!-- L9_META"
CLOSE = "/L9_META -->"

RE_OPEN = re.compile(r"^<!--[ \t]*L9_META[ \t]*$")
RE_CLOSE = re.compile(r"^[ \t]*/L9_META[ \t]*-->[ \t]*$")

RE_FRONT_MATTER = re.compile(r"^---[ \t]*\n.*?^(?:---|\.\.\.)[ \t]*\n", re.MULTILINE | re.DOTALL)


def render(rec: MetaRecord) -> str:
    return "\n".join([OPEN, *field_lines(rec), CLOSE])


def split_front_matter(text: str) -> tuple[str, str]:
    """Return (front_matter, remainder). Front matter is empty when absent."""
    if not text.startswith("---"):
        return "", text
    match = RE_FRONT_MATTER.match(text)
    if not match:
        return "", text
    return match.group(0), text[match.end() :]


def _find_block(lines: list[str]) -> tuple[int, int] | None:
    for i, line in enumerate(lines):
        if RE_OPEN.match(line):
            for j in range(i + 1, len(lines)):
                if RE_CLOSE.match(lines[j]):
                    return i, j + 1
            return None
    return None


def parse(text: str) -> MetaRecord | None:
    _, remainder = split_front_matter(text)
    lines = remainder.splitlines()
    span = _find_block(lines)
    if span is None:
        return None
    start, end = span
    return record_from_fields(fields_from_lines(lines[start + 1 : end - 1]))


def strip(text: str) -> str:
    front_matter, remainder = split_front_matter(text)
    trailing_newline = remainder.endswith("\n")
    lines = remainder.splitlines()
    while True:
        span = _find_block(lines)
        if span is None:
            break
        start, end = span
        del lines[start:end]
    result = "\n".join(lines)
    if trailing_newline and result:
        result += "\n"
    return front_matter + result


def inject(text: str, rec: MetaRecord) -> str:
    stripped = strip(text)
    front_matter, remainder = split_front_matter(stripped)
    block = render(rec)
    body = remainder.lstrip("\n")
    return front_matter + block + "\n\n" + body
