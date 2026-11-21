"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, ci]
tags: [test, unit, ci, ruff]
owner: engine-team
status: active
--- /L9_META ---

Witness that ruff, which the required Analyze (central Core) runs in
`make check`, bans print() outside the command-line programs and enforces the
deprecated-import ban (TID251) everywhere, tools/ included.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PRINT_ALLOWED = {"tools/**", "scripts/**", "chassis/auth/generate_l9_api_key.py"}


def _lint() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]


def test_print_is_selected() -> None:
    assert "T20" in _lint()["select"]


def test_print_is_allowed_only_in_command_line_programs() -> None:
    allowed = {glob for glob, codes in _lint()["per-file-ignores"].items() if {"T20", "T201"} & set(codes)}
    assert allowed == PRINT_ALLOWED


def test_banned_api_is_ignored_nowhere() -> None:
    lint = _lint()
    assert "TID251" not in lint["ignore"]
    assert not [glob for glob, codes in lint["per-file-ignores"].items() if "TID251" in codes]
