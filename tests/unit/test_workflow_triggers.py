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

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


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


# One local owner per check. The required Analyze (central Core) job runs the
# same Repo.mk leaves (`make validate`, `make check`) on every pull request.
LOCAL_OWNERS = {
    "ruff check": "l9-lint-test.yml",
    "ruff format --check": "l9-lint-test.yml",
    "mypy engine/": "l9-lint-test.yml",
    "tools/contract_scanner.py": "contracts.yml",
    "tools/verify_contracts.py": "contracts.yml",
    "tools/l9_meta_injector.py": "contracts.yml",
    "tools/check_action_refs.py": "contracts.yml",
}


# Not a second owner: lint-autofix.yml rewrites main and opens a fix PR;
# refactoring-validation.yml runs only on refactor/* branches.
NOT_CHECK_OWNERS = {"lint-autofix.yml", "refactoring-validation.yml"}


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_each_local_check_has_one_workflow_owner() -> None:
    """ruff and mypy once ran in seven places per PR; each local check now has one owner."""
    workflows = [path for path in sorted(WORKFLOWS.glob("*.yml")) if path.name not in NOT_CHECK_OWNERS]
    for command, owner in LOCAL_OWNERS.items():
        runners = [path.name for path in workflows if command in _code(path)]
        assert runners == [owner], f"{command}: {runners}"
