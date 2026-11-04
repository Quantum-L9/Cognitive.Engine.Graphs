"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, native-domain, runbook, cli]
owner: engine-team
status: active
--- /L9_META ---

Smoke the runbook's native validation example without publication or approval."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_domain_pack_loader import _minimal_spec_yaml

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]


def test_runbook_absolute_candidate_validation_command(tmp_path: Path) -> None:
    runbook = (REPO / "docs/runbooks/domain-pack-publish.md").read_text(encoding="utf-8")
    commands = runbook.replace("\\\n", " ").splitlines()
    example = next(
        line
        for line in commands
        if line.startswith(".venv/bin/python tools/validate_domain.py /operator/input/candidate.yaml")
    )
    # A disposable stage-marked specimen is not approval; no publisher is called.
    data = _minimal_spec_yaml("acme").replace("0.0.1", "1.0.0-reviewed").encode()
    candidate = tmp_path / "candidate.yaml"
    candidate.write_bytes(data)
    substitutions = {"/operator/input/candidate.yaml": str(candidate), "/operator/input": str(tmp_path)}
    command = [substitutions.get(token, token) for token in shlex.split(example)]
    assert command[:2] == [".venv/bin/python", "tools/validate_domain.py"]
    assert command[2] == str(candidate)
    env = os.environ.copy()
    env.update(
        L9_ENV="dev",
        DOMAIN_PACK_RECEIPTS_ENABLED="false",
        DOMAIN_PRODUCTION_STAGE_REQUIRED="false",
        DOMAIN_STRICT_VALIDATION="true",
    )
    result = subprocess.run(  # noqa: S603 — verified repo-owned CLI example; no shell or publisher.
        command,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    body = json.loads(result.stdout)
    assert body["exit_code"] == result.returncode
    assert body["spec_path"] == str(candidate)
    assert body["results"][0]["check"] == "native-admission"
    assert body["results"][0]["status"] == "PASS"
    assert command[command.index("--root") + 1] == str(tmp_path)
    assert candidate.read_bytes() == data
    assert not list(tmp_path.rglob("current.json"))
    assert not list(tmp_path.rglob("receipt.json"))
