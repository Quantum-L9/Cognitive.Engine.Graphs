"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tools, security]
tags: [path-policy, cli, filesystem-boundary]
owner: engine-team
status: active
--- /L9_META ---

Shared explicit-root policy for CEG command-line tools.

Operator-selected paths are resolved against a declared root and rejected when
resolution would escape it, including through an existing symlink. Relative
paths are interpreted relative to that root, not the process working directory.
"""

from __future__ import annotations

from pathlib import Path


class PathPolicyError(ValueError):
    """Raised when an operator-selected path violates a tool's allowed root."""


def resolve_root(value: str | Path) -> Path:
    """Resolve and validate a root directory supplied by a CLI caller."""
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise PathPolicyError(f"--root must be an existing directory: {root}")
    return root


def resolve_under_root(value: str | Path, root: Path, *, option: str) -> Path:
    """Resolve *value* and reject it unless it remains under the allowed *root*."""
    supplied = Path(value).expanduser()
    candidate = supplied if supplied.is_absolute() else root / supplied
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise PathPolicyError(f"{option} must stay under --root ({root})")
    return resolved


def resolve_existing_file_under_root(value: str | Path, root: Path, *, option: str) -> Path:
    """Resolve an existing regular file under *root* for a CLI input argument."""
    resolved = resolve_under_root(value, root, option=option)
    if not resolved.is_file():
        raise FileNotFoundError(f"{option} must name an existing file: {resolved}")
    return resolved
