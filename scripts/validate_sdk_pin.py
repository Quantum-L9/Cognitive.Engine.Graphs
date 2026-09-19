#!/usr/bin/env python3
"""Fail closed unless Gate_SDK is pinned to the moving major tag.

Policy (CEG#266 / PR #263): constellation-node-sdk tracks ``Quantum-L9/Gate_SDK@v1``.
Manifests name the major tag. poetry.lock records that tag as ``reference`` and
the resolved object as ``resolved_reference``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAJOR_TAG = "v1"
CANONICAL_REPO = "Quantum-L9/Gate_SDK"
FORBIDDEN_FORK = "cryptoxdog/Gate_SDK"
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
LOCK_REFERENCE_RE = re.compile(
    r'name = "constellation-node-sdk".*?\[package\.source\].*?'
    r'reference = "([^"]+)".*?resolved_reference = "([0-9a-f]{40})"',
    re.DOTALL,
)


def check_text(rel: str, text: str) -> list[str]:
    errors: list[str] = []
    if CANONICAL_REPO not in text:
        errors.append(f"{rel}: missing {CANONICAL_REPO}")
    if FORBIDDEN_FORK in text:
        errors.append(f"{rel}: {FORBIDDEN_FORK} remains")
    if rel == "poetry.lock":
        match = LOCK_REFERENCE_RE.search(text)
        if match is None:
            errors.append(f"{rel}: constellation-node-sdk source block missing")
        else:
            reference, resolved = match.group(1), match.group(2)
            if reference != MAJOR_TAG:
                errors.append(f"{rel}: reference={reference!r} (want {MAJOR_TAG!r})")
            if not SHA_RE.fullmatch(resolved):
                errors.append(f"{rel}: resolved_reference is not a SHA")
    else:
        if MAJOR_TAG not in text:
            errors.append(f"{rel}: missing moving major tag {MAJOR_TAG}")
        if SHA_RE.search(text):
            errors.append(f"{rel}: immutable SHA pin leftover")
    return errors


def check_tree(root: Path) -> list[str]:
    errors: list[str] = []
    for rel in ("pyproject.toml", "requirements.txt", "poetry.lock"):
        path = root / rel
        if not path.exists():
            continue
        errors.extend(check_text(rel, path.read_text(encoding="utf-8")))
    return errors


def main() -> int:
    errors = check_tree(ROOT)
    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1
    print(f"PASS CEG pin {CANONICAL_REPO}@{MAJOR_TAG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
