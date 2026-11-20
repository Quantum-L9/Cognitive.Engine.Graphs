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
    assert "pull_request" in triggers
    assert triggers["push"] == {"branches": ["main"]}
