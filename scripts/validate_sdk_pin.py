#!/usr/bin/env python3
"""Fail closed unless Gate_SDK is pinned to the moving major tag.

Policy (CEG#266 / PR #263): constellation-node-sdk tracks ``Quantum-L9/Gate_SDK@v1``.
Manifests name the major tag. poetry.lock records that tag as ``reference`` and
the resolved object as ``resolved_reference``.

Gate_SDK owns what ``v1`` means (``contracts/RELEASE_IDENTITY_LEDGER.json``,
schema v2). The structural checks below prove CEG declares the channel; they
cannot prove the lock still holds what the channel points at, because a moving
tag goes stale without any local file changing. ``--verify-tag`` closes that
gap by resolving the channel at the canonical remote.

Modes
-----
default        Offline structural checks over the active surfaces.
--verify-tag   Resolves ``v1`` at the canonical remote and requires
               ``resolved_reference`` to match. Fails closed when the remote
               cannot be resolved — a lock that cannot be checked has not been
               checked.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAJOR_TAG = "v1"
CANONICAL_REPO = "Quantum-L9/Gate_SDK"
CANONICAL_REMOTE = f"https://github.com/{CANONICAL_REPO}.git"
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


def lock_resolution(text: str) -> str | None:
    """The concrete object poetry recorded for the moving tag."""
    match = LOCK_REFERENCE_RE.search(text)
    return match.group(2) if match else None


def resolve_remote_tag(remote: str, tag: str) -> str | None:
    """Resolve ``refs/tags/<tag>`` at *remote*, preferring the peeled object."""
    completed = subprocess.run(
        ["/usr/bin/git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    peeled: str | None = None
    direct: str | None = None
    for line in completed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name == f"refs/tags/{tag}^{{}}":
            peeled = sha.strip()
        elif name == f"refs/tags/{tag}":
            direct = sha.strip()
    return peeled or direct


def compare_lock_to_tag(resolved: str | None, tag_sha: str | None) -> list[str]:
    """Stale-lock detection: the lock must hold what the channel points at now."""
    if tag_sha is None:
        return [f"--verify-tag: could not resolve {CANONICAL_REPO}@{MAJOR_TAG}; an unverifiable lock does not pass"]
    if resolved is None:
        return ["--verify-tag: poetry.lock carries no resolved_reference to compare"]
    if resolved != tag_sha:
        return [
            (
                f"--verify-tag: poetry.lock resolved_reference is {resolved} but "
                f"{MAJOR_TAG} now points at {tag_sha} — the lock is stale; re-run `poetry lock`"
            )
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Gate_SDK moving-major pin.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--verify-tag",
        action="store_true",
        help="resolve the channel at the canonical remote and compare to poetry.lock",
    )
    parser.add_argument("--remote", default=CANONICAL_REMOTE)
    args = parser.parse_args(argv)

    errors = check_tree(args.root)

    if args.verify_tag:
        lock_path = args.root / "poetry.lock"
        resolved = lock_resolution(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else None
        tag_sha = resolve_remote_tag(args.remote, MAJOR_TAG)
        errors.extend(compare_lock_to_tag(resolved, tag_sha))
        if not errors:
            print(f"NETWORK: {CANONICAL_REPO}@{MAJOR_TAG} == poetry.lock {tag_sha}")

    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1
    mode = "offline + networked" if args.verify_tag else "offline"
    print(f"PASS CEG pin {CANONICAL_REPO}@{MAJOR_TAG} ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
