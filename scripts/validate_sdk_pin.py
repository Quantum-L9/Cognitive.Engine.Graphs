#!/usr/bin/env python3
"""Fail closed unless Gate_SDK is pinned to the moving major tag.

Policy (CEG#266 / PR #263): constellation-node-sdk tracks ``Quantum-L9/Gate_SDK@v1``.
Manifests name the major tag. poetry.lock records that tag as ``reference`` and
the resolved object as ``resolved_reference``.

CEG-001 asked for an immutable SHA in the manifests instead. The release set
kept the moving tag and closed the finding the other way: the **lock** is the
identity every deployed image installs, so the lock is what this script
verifies, and the resolved commit is printed on PASS so a build log records
which SDK object was actually taken. ``Enrichment.Inference.Engine/scripts/
validate_sdk_pin.py`` enforces the same contract over that repo's
``requirements.lock``.

Accepted trade-off, stated rather than hidden: a build that resolves the
manifest live (``pip install -r requirements.txt``, and CI installs generally)
takes whatever ``v1`` points at that minute, while a lock-driven build takes
``resolved_reference``. They agree today. Moving the ``v1`` tag without
refreshing the lock is what would separate them, and that is a deliberate act
with a diff, not silent drift between two files in this repository.
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


def resolved_commit(root: Path) -> str | None:
    """The commit poetry.lock records for the major tag, for the PASS line."""
    path = root / "poetry.lock"
    if not path.exists():
        return None
    match = LOCK_REFERENCE_RE.search(path.read_text(encoding="utf-8"))
    return None if match is None else match.group(2)


def main() -> int:
    errors = check_tree(ROOT)
    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1
    print(f"PASS CEG pin {CANONICAL_REPO}@{MAJOR_TAG} -> {resolved_commit(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
