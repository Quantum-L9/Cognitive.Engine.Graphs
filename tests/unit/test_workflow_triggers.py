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


def _matches(pattern: str) -> bool:
    return next(ROOT.glob(pattern), None) is not None


def test_no_workflow_has_a_path_filter_that_can_never_match() -> None:
    """dev-layer-gmp.yml filtered on two paths that never existed, so it never ran a test."""
    dead = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        triggers = _triggers(path.name) or {}
        if not isinstance(triggers, dict):
            continue
        for event, config in triggers.items():
            patterns = config.get("paths") if isinstance(config, dict) else None
            if patterns and not any(_matches(pattern) for pattern in patterns):
                dead.append(f"{path.name}:{event}")
    assert dead == []
