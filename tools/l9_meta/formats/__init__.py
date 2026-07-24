"""Format dispatch: path -> the module that knows how to read and write its header."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from tools.l9_meta.formats import comment, docstring, html, jsonmeta, tomlmeta
from tools.l9_meta.model import MetaRecord


class Format(Protocol):
    """The four operations every format implements."""

    def render(self, rec: MetaRecord) -> Any: ...
    def parse(self, text: str) -> MetaRecord | None: ...
    def strip(self, text: str) -> str: ...
    def inject(self, text: str, rec: MetaRecord) -> str: ...


FORMATS: dict[str, Any] = {
    "comment": comment,
    "docstring": docstring,
    "html": html,
    "json": jsonmeta,
    "toml": tomlmeta,
}

# Extensions that carry no comment syntax at all.
UNSUPPORTED_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".lock",
        ".csv",
        ".txt",
        ".jsonl",
    }
)


SUFFIX_FORMATS: dict[str, str] = {
    ".yaml": "comment",
    ".yml": "comment",
    ".sh": "comment",
    ".bash": "comment",
    ".zsh": "comment",
    ".template": "comment",
    ".env": "comment",
    ".cfg": "comment",
    ".ini": "comment",
    ".conf": "comment",
    ".py": "docstring",
    ".md": "html",
    ".mdc": "html",
    ".json": "json",
    ".toml": "toml",
}

# Extensionless or dotfile names that use `#` comments.
COMMENT_NAMES = frozenset({"codeowners", "makefile"})
COMMENT_NAME_PREFIXES = ("dockerfile", ".cursorrules", ".gitignore", ".env", ".dockerignore")


def detect_filetype(path: str | Path) -> str | None:
    """Return the format name for a path, or None when unsupported.

    Suffixes are lowercased, so `docs/ACTION ITEMS.MD` resolves as markdown.
    """
    pure = PurePosixPath(str(path).replace("\\", "/"))
    name = pure.name.lower()
    suffix = pure.suffix.lower()

    if suffix in UNSUPPORTED_SUFFIXES:
        return None
    if name in COMMENT_NAMES or name.startswith(COMMENT_NAME_PREFIXES):
        return "comment"
    if suffix in SUFFIX_FORMATS:
        return SUFFIX_FORMATS[suffix]
    if ".template" in name:
        return "comment"
    return None


def for_path(path: str | Path) -> Any | None:
    """Return the format module for a path, or None when unsupported."""
    ftype = detect_filetype(path)
    return FORMATS.get(ftype) if ftype else None


__all__ = ["FORMATS", "Format", "detect_filetype", "for_path"]
