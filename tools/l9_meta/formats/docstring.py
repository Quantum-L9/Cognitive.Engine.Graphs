"""Docstring-style L9_META blocks for Python modules.

Canonical form — first lines of the module docstring:

    \"\"\"
    --- L9_META ---
    l9_schema: 2
    ...
    --- /L9_META ---

    Original docstring prose follows.
    \"\"\"

Python files also get comment-style stripping, because the legacy bare
`# L9_META` form appears above the docstring in some modules.
"""

from __future__ import annotations

import re

from tools.l9_meta.formats import comment
from tools.l9_meta.model import (
    MetaRecord,
    field_lines,
    fields_from_lines,
    record_from_fields,
)

OPEN = "--- L9_META ---"
CLOSE = "--- /L9_META ---"

RE_OPEN = re.compile(r"^[ \t]*---[ \t]*L9_META[ \t]*---[ \t]*$")
RE_CLOSE = re.compile(r"^[ \t]*---[ \t]*/L9_META[ \t]*---[ \t]*$")

RE_SHEBANG = re.compile(r"^#![^\n]*\n")
RE_DOCSTRING = re.compile(r'^(?P<quote>"""|\'\'\')(?P<body>.*?)(?P=quote)', re.DOTALL)


def render(rec: MetaRecord) -> str:
    return "\n".join([OPEN, *field_lines(rec), CLOSE])


def _find_block(lines: list[str]) -> tuple[int, int] | None:
    for i, line in enumerate(lines):
        if RE_OPEN.match(line):
            for j in range(i + 1, len(lines)):
                if RE_CLOSE.match(lines[j]):
                    return i, j + 1
            return None
    return None


def parse(text: str) -> MetaRecord | None:
    lines = text.splitlines()
    span = _find_block(lines)
    if span is not None:
        start, end = span
        return record_from_fields(fields_from_lines(lines[start + 1 : end - 1]))
    return comment.parse(text)


def strip(text: str) -> str:
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    while True:
        span = _find_block(lines)
        if span is None:
            break
        start, end = span
        del lines[start:end]
    result = "\n".join(lines)
    if trailing_newline and result:
        result += "\n"
    return comment.strip(_drop_empty_docstring(result))


RE_EMPTY_DOCSTRING = re.compile(r'^(?P<pre>#![^\n]*\n)?(?P<quote>"""|\'\'\')\s*(?P=quote)\n?')


def _drop_empty_docstring(text: str) -> str:
    """Remove a docstring left with no content after the block was stripped.

    A header-only docstring collapses to `\"\"\"\\n\"\"\"` once stripped. Leaving it
    is not cosmetic: an empty string literal is still a statement, so a real
    docstring below it gets demoted and `from __future__` stops being first.
    """
    match = RE_EMPTY_DOCSTRING.match(text)
    if not match:
        return text
    return (match.group("pre") or "") + text[match.end() :]


def _split_shebang(text: str) -> tuple[str, str]:
    match = RE_SHEBANG.match(text)
    if match:
        return match.group(0), text[match.end() :]
    return "", text


def _split_preamble(text: str) -> tuple[str, str]:
    """Split off leading comment and blank lines.

    Comments are not statements, so a module docstring may sit below them and
    still be the docstring. Matching the docstring only at byte 0 misses that
    case and prepends a second string literal instead — which demotes the real
    docstring to an expression statement and makes any `from __future__ import`
    below it a SyntaxError.
    """
    lines = text.splitlines(keepends=True)
    cut = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            cut = i + 1
            continue
        break
    return "".join(lines[:cut]), "".join(lines[cut:])


def inject(text: str, rec: MetaRecord) -> str:
    shebang, remainder = _split_shebang(strip(text))
    remainder = remainder.lstrip("\n")
    block = render(rec)

    preamble, body_text = _split_preamble(remainder)
    match = RE_DOCSTRING.match(body_text)
    if match:
        quote = match.group("quote")
        body = match.group("body").strip("\n")
        tail = body_text[match.end() :]
        docstring = f"{quote}\n{block}\n\n{body}\n{quote}" if body else f"{quote}\n{block}\n{quote}"
        return shebang + preamble + docstring + tail

    # A blank line must follow a module docstring we are introducing, or
    # `ruff format --check` reformats the file and CI fails. Only the new-docstring
    # branch needs this; the merge branch above preserves whatever spacing the
    # original docstring already had.
    separator = "\n" if remainder else ""
    return shebang + f'"""\n{block}\n"""\n' + separator + remainder
