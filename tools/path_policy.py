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

import errno
import os
import stat
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


def write_text_no_follow(directory: Path, child: str, text: str) -> None:
    """Write a regular child of a validated, resolved directory without symlink follows.

    Open/create each directory component relative to an owned descriptor, then
    open the child with O_NOFOLLOW. The helper owns every descriptor, including
    while fdopen constructs, writes, flushes or closes its non-owning wrapper.
    """
    if child in {"", ".", ".."} or Path(child).name != child:
        raise PathPolicyError("output must name a single child file")
    directory = directory.absolute()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    owned_fds: list[int] = []
    try:
        owned_fds.append(os.open(directory.anchor, directory_flags))
        for component in directory.parts[1:]:
            parent_fd = owned_fds[-1]
            try:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, dir_fd=parent_fd)
                except FileExistsError:
                    pass  # A concurrent creator still has to pass the no-follow open.
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            owned_fds.append(next_fd)

        file_fd = os.open(
            child,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o666,
            dir_fd=owned_fds[-1],
        )
        owned_fds.append(file_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise PathPolicyError("output must be a regular child file")
        os.ftruncate(file_fd, 0)
        with os.fdopen(file_fd, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(text)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathPolicyError("output child must not be a symlink") from exc
        raise
    finally:
        for fd in reversed(owned_fds):
            os.close(fd)
