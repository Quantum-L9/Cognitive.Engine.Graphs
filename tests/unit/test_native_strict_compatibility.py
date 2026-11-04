"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-authoring, predecessor-parity]
owner: engine-team
status: active
--- /L9_META ---

F-001 authoring CLI compatibility; native runtime admission remains distinct."""

# ruff: noqa: S603 — current interpreter and repository-owned CLIs only.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.test_domain_pack_loader import _minimal_spec_yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

AUTHORING_ENV = {
    "L9_ENV": "dev",
    "UNVALIDATED_DOMAIN_PACKS_ENABLED": "true",
    "DORMANT_DOMAIN_PACKS_ENABLED": "true",
    "DOMAIN_STRICT_VALIDATION": "true",
    "SCORING_ADMISSION_CHECK_ENABLED": "false",
    "ADMISSION_CHECKS_FULL_MATCH": "false",
}


def _source(tmp_path: Path, case: str = "good") -> Path:
    raw = yaml.safe_load(_minimal_spec_yaml("acme"))
    raw["ontology"]["edges"] = [
        {
            "type": "ASKS",
            "from": "Ask",
            "to": "Thing",
            "direction": "DIRECTED",
            "category": "capability",
            "managedby": "sync",
        }
    ]
    raw["traversal"]["steps"] = [
        {"name": "ask", "pattern": "(query:Ask)-[:ASKS]->(candidate:Thing)", "matchdirections": ["d1"]}
    ]
    if case == "traversal":
        raw["traversal"]["steps"][0]["pattern"] = "(query:Ask)-[:MISSING]->(candidate:Thing)"
    elif case == "cross-reference":
        raw["gates"] = [{"name": "edge", "type": "traversal", "edgetype": "MISSING"}]
    elif case == "edge-source":
        raw["ontology"]["edges"][0]["from"] = "Missing"
    elif case == "edge-target":
        raw["ontology"]["edges"][0]["to"] = "Missing"
    elif case == "candidate":
        raw["matchentities"]["candidate"][0]["label"] = "Missing"
    elif case == "gate":
        raw["gates"] = [{"name": "broken", "type": "composite", "subgates": ["missing"]}]
    elif case == "compliance":
        raw["gates"] = [{"name": "blocked", "type": "boolean", "candidateprop": "thing_id"}]
        raw["compliance"] = {"prohibitedfactors": {"enabled": True, "blockedfields": ["thing_id"]}}
    path = tmp_path / "acme" / "spec.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _cli(path: Path, *flags: str, script: Path | None = None) -> tuple[int, dict[str, Any]]:
    env = os.environ.copy()
    # Pin ordinary local diagnostic defaults; only runtime bypasses are enabled.
    # The predecessor authoring checks must not be bypassed by these switches.
    env.update(PYTHONPATH=str(REPO), **AUTHORING_ENV)
    root_flags = (
        [] if script is not None else ["--root", str(path.parent.parent if path.name == "spec.yaml" else path.parent)]
    )
    result = subprocess.run(
        [sys.executable, str(script or REPO / "tools/validate_domain.py"), str(path), "--json", *root_flags, *flags],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert "Traceback" not in result.stderr
    body = json.loads(result.stdout)
    assert body["exit_code"] == result.returncode
    return result.returncode, body


@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_actual_authoring_cli_runs_all_mandatory_checks(tmp_path: Path, flags: tuple[str, ...]) -> None:
    code, body = _cli(_source(tmp_path), *flags)
    assert code == 0
    assert {item["check"] for item in body["results"]} >= {
        "W1-01-cross-ref",
        "W1-03-gate-compile",
        "W1-04-traversal",
        "W3-02-prohibited-factors",
    }


@pytest.mark.parametrize(("flags", "expected"), [((), 2), (("--strict",), 1)])
def test_actual_authoring_cli_warning_exit_and_strict_escalation(
    tmp_path: Path, flags: tuple[str, ...], expected: int
) -> None:
    code, body = _cli(_source(tmp_path, "traversal"), *flags)
    assert code == expected
    assert any(item["check"] == "W1-04-traversal" and item["status"] == "WARN" for item in body["results"])


@pytest.mark.parametrize("case", ["cross-reference", "edge-source", "edge-target", "candidate", "gate", "compliance"])
@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_actual_authoring_cli_failures_cannot_use_runtime_bypasses(
    tmp_path: Path, case: str, flags: tuple[str, ...]
) -> None:
    code, body = _cli(_source(tmp_path, case), *flags)
    assert code == 1
    assert any(item["status"] == "FAIL" for item in body["results"])
    named_checks = {
        "cross-reference": "W1-01-gate-edge",
        "gate": "W1-03-gate-compile",
        "compliance": "W3-02-prohibited-factors",
    }
    if case in named_checks:
        assert any(item["check"] == named_checks[case] and item["status"] == "FAIL" for item in body["results"])
