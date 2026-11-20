"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, ci]
tags: [test, unit, ci, workflows]
owner: engine-team
status: active
--- /L9_META ---

Witnesses that a workflow does not run twice for one commit under review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _triggers(name: str) -> dict:
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    # YAML 1.1 reads the bare key `on` as boolean True.
    return document.get("on", document.get(True))


def test_contracts_push_runs_only_on_main() -> None:
    """An unfiltered push trigger ran every contracts job twice per commit on a PR branch."""
    triggers = _triggers("contracts.yml")
    # No branch or path filter: every PR, whatever its base, still runs the contract jobs.
    assert "pull_request" in triggers
    assert not triggers["pull_request"]
    assert triggers["push"] == {"branches": ["main"]}


# Directories that are never repository content: VCS metadata, the environment
# `make setup` creates, and Core's runtime scaffolding.
_NOT_REPOSITORY = {".git", ".venv", "node_modules", "__pycache__"}


def _repository_files() -> list[str]:
    files = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if _NOT_REPOSITORY & set(relative.parts) or relative.parts[:2] == (".l9", "runtime"):
            continue
        if path.is_file():
            files.append(relative.as_posix())
    return files


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """GitHub filter glob: `**` crosses `/`, `*` and `?` do not, and a pattern matches whole file paths."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _selected(patterns: list[str], file: str) -> bool:
    """Apply the patterns in order, as GitHub does: a later `!pattern` excludes, a later pattern re-includes."""
    selected = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if _glob_regex(pattern[1:] if negated else pattern).match(file):
            selected = not negated
    return selected


def test_path_filter_matcher_follows_github_semantics() -> None:
    assert _selected(["docs/**"], "docs/a/b.md")
    assert not _selected(["docs"], "docs/a.md"), "a directory name does not match the files in it"
    assert not _selected(["docs/*"], "docs/a/b.md"), "a single * does not cross /"
    assert _selected(["**/*.py"], "engine/x.py")
    assert _selected(["**/*.py"], "x.py")
    assert not _selected(["docs/**", "!docs/**"], "docs/a.md"), "a later negation excludes"
    assert _selected(["docs/**", "!docs/**", "docs/a.md"], "docs/a.md"), "a later pattern re-includes"


def test_no_workflow_has_a_path_filter_that_can_never_match() -> None:
    """dev-layer-gmp.yml filtered on two paths that never existed, so it never ran a test."""
    files = _repository_files()
    dead = []
    workflows = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    for path in workflows:
        triggers = _triggers(path.name) or {}
        if not isinstance(triggers, dict):
            continue
        for event, config in triggers.items():
            patterns = config.get("paths") if isinstance(config, dict) else None
            if patterns and not any(_selected(patterns, file) for file in files):
                dead.append(f"{path.name}:{event}")
    assert dead == []
