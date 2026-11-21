"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, ci]
tags: [test, unit, ci, mypy]
owner: engine-team
status: active
--- /L9_META ---

Witness that the required check type-checks every production Python package.

`Analyze (central Core)` is the only merge-blocking check on CEG, and it type
checks through `Repo.mk` `repo-check`. When that target covered `engine/` only,
`chassis/` (the HTTP adapter) and `scripts/` were type-checked by no CI job at
all, so a narrower target is a silent protection loss.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_TARGETS = {"engine/", "chassis/", "scripts/"}


def _repo_check_mypy_targets() -> set[str]:
    text = (ROOT / "Repo.mk").read_text(encoding="utf-8")
    recipe = re.search(r"^repo-check:.*?\n((?:\t.*\n)+)", text, re.M)
    assert recipe, "Repo.mk has no repo-check recipe"
    targets: set[str] = set()
    for line in recipe.group(1).splitlines():
        words = shlex.split(line.strip().lstrip("@"))
        if "mypy" in words:
            targets.update(w for w in words[words.index("mypy") + 1 :] if not w.startswith("-"))
    return targets


def test_repo_check_type_checks_every_production_package() -> None:
    assert _repo_check_mypy_targets() >= REQUIRED_TARGETS


def test_every_required_target_holds_python() -> None:
    for target in REQUIRED_TARGETS:
        assert any((ROOT / target).rglob("*.py")), target
