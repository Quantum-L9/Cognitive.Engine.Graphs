#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [tools, security]
# tags: [cypher, lint, C-009, injection]
# owner: platform
# status: active
# --- /L9_META ---
"""Scan engine/**/*.py for unparameterized Cypher interpolations (C-009).

Fails on:
- quoted f-string value interpolations (``= '{x}'`` / ``'{var}'`` in Cypher)
- ``LIMIT {n}`` without ``$``
- label interpolations whose expression is not ``sanitize_label(...)``
  (and whose name was not assigned from ``sanitize_label``)

Skips raise / logger / msg= / ValueError lines. Cypher tokens are
case-sensitive so English "does not match" is not a hit.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

CYPHER_TOKEN_RE = re.compile(r"\b(?:MATCH|MERGE|CREATE|SET|WHERE|CALL|LIMIT|UNWIND|THEN|OPTIONAL)\b")
SKIP_LINE_RE = re.compile(r"\b(?:raise|logger|ValueError)\b|msg\s*=")
QUOTED_INTERP_RE = re.compile(r"'\{[^{}]+\}'")
LIMIT_INTERP_RE = re.compile(r"\bLIMIT\s*(?!\$)\{")
LABEL_INTERP_RE = re.compile(r":\{([^{}]+)\}")
SANITIZE_ASSIGN_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:sanitize_label\s*\(|self\._get_candidate_label\s*\(|\[sanitize_label\b)"
)


@dataclass(frozen=True)
class Finding:
    rel_path: str
    line_no: int
    pattern: str
    kind: str = "Unparameterized value interpolation detected"


def _fstring_lines(source: str) -> set[int]:
    """Line numbers that participate in an f-string.

    Python 3.12+ emits FSTRING_* tokens; 3.9-3.11 still use STRING with an
    ``f``/``F`` prefix. ``make cypher-lint`` must run on both.
    """
    lines: set[int] = set()
    fstring_types = {
        getattr(tokenize, name)
        for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if getattr(tokenize, name, None) is not None
    }
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type in fstring_types or (
                tok.type == tokenize.STRING and tok.string[:1] in "fF"
            ):
                for ln in range(tok.start[0], tok.end[0] + 1):
                    lines.add(ln)
    except (tokenize.TokenError, SyntaxError):
        for idx, line in enumerate(source.splitlines(), start=1):
            if re.search(r"\bf['\"]", line):
                lines.add(idx)
    return lines


def _sanitized_names(source: str) -> set[str]:
    return set(SANITIZE_ASSIGN_RE.findall(source))


def _label_is_safe(expr: str, sanitized: set[str]) -> bool:
    stripped = expr.strip()
    if "sanitize_label" in stripped:
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", stripped) and stripped in sanitized:
        return True
    return False


def scan_file(path: Path, *, root: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    fstring_lines = _fstring_lines(source)
    sanitized = _sanitized_names(source)
    findings: list[Finding] = []

    for line_no, line in enumerate(source.splitlines(), start=1):
        if line_no not in fstring_lines:
            continue
        if SKIP_LINE_RE.search(line):
            continue
        if not CYPHER_TOKEN_RE.search(line):
            continue

        stripped = line.strip()
        if QUOTED_INTERP_RE.search(line):
            findings.append(Finding(rel, line_no, stripped))
            continue
        if LIMIT_INTERP_RE.search(line):
            findings.append(Finding(rel, line_no, stripped))
            continue
        for match in LABEL_INTERP_RE.finditer(line):
            if not _label_is_safe(match.group(1), sanitized):
                findings.append(Finding(rel, line_no, stripped))
                break

    return findings


def scan_tree(root: Path) -> list[Finding]:
    engine = root / "engine"
    if not engine.is_dir():
        return []
    findings: list[Finding] = []
    for path in sorted(engine.rglob("*.py")):
        if not path.is_file():
            continue
        findings.extend(scan_file(path, root=root))
    return findings


def render_findings(findings: list[Finding]) -> str:
    blocks = []
    for item in findings:
        blocks.append(
            f"❌ {item.kind}\nFile: {item.rel_path}:{item.line_no}\nPattern: {item.pattern}"
        )
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else Path.cwd()
    findings = scan_tree(root)
    if findings:
        print(render_findings(findings))
        return 1
    print("OK: cypher-lint — 0 injection vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
