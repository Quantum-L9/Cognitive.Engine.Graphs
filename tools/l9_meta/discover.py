"""Enumerate the files the pipeline is allowed to touch.

Enumeration goes through `git ls-files -z`. Null termination is required, not
cosmetic: 14 tracked paths contain spaces (`docs/ACTION ITEMS.MD`,
`docs/PlasticOS Graph Cognitive Engine.yaml`, ...) and a whitespace split
silently drops or halves them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.l9_meta.formats import detect_filetype


class DiscoveryError(RuntimeError):
    """Raised when the git index cannot be read."""


def git_tracked(root: Path) -> list[str]:
    """Return every path in the git index, as posix-relative strings."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        msg = f"cannot read git index at {root}: {exc}"
        raise DiscoveryError(msg) from exc
    return [entry for entry in proc.stdout.split("\0") if entry]


def eligible_files(root: Path, excludes: list[str]) -> list[str]:
    """Tracked files that exist on disk, are not excluded, and have a known format.

    Staged deletions remain in the index, so the existence filter is what keeps
    the pipeline from reporting phantom work.
    """
    from tools.l9_meta.resolve import matches

    out: list[str] = []
    for rel in git_tracked(root):
        if not (root / rel).is_file():
            continue
        if any(matches(pattern, rel) for pattern in excludes):
            continue
        if detect_filetype(rel) is None:
            continue
        out.append(rel)
    return sorted(out)
