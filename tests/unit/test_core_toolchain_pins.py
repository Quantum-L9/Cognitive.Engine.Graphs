"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, ci, dependencies, toolchain]
owner: engine-team
status: active
--- /L9_META ---

CEG declares no Core-owned tool version, so a Core bump needs no edit here.

Quantum-L9/l9-ci-core owns the versions of the shared tools
(.github/actions/install-consumer-ci/toolchain-lock.json); version drift from
Core is prohibited org-wide. CEG therefore names none of them: they are absent
from pyproject.toml, excluded from uv.lock ([tool.uv] exclude-dependencies,
which also stops pytest plugins and pip-audit pulling them in transitively),
run by local system pre-commit hooks, and installed by
tools/deps/install_locked.sh from Core's installer pins at the v2 tag. Core's
pin-file PR plus the v2 move is the whole bump.

They also keep the two Dependabot rules that stop drift recurring: Core-owned
tools are ignored, and every github/codeql-action step runs one version.

Which tools Core owns, and at which versions, is read from Core itself by
scripts/check_core_toolchain.py (networked, ci.yml Gate_SDK Channel), never
from a list kept here; tests/unit/test_check_core_toolchain.py tests its rules
offline. This file keeps the checks that need no Core metadata.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

_INSTALLER_PINS = (
    "https://raw.githubusercontent.com/Quantum-L9/l9-ci-core/${CORE_REF}/"
    ".github/actions/install-consumer-ci/requirements-consumer-ci.txt"
)


def test_install_script_takes_core_tools_from_core() -> None:
    script = (ROOT / "tools" / "deps" / "install_locked.sh").read_text(encoding="utf-8")
    assert 'CORE_REF="${L9_CI_CORE_REF:-v2}"' in script
    assert _INSTALLER_PINS in script
    assert "--only-binary :all:" in script
    # Without --no-config, `uv pip` applies exclude-dependencies and installs nothing.
    assert "uv pip install --no-config" in script
    # The CI and Repo.mk profiles that run ruff, mypy, pytest and pre-commit.
    assert re.search(r"^\s*ci\|full\) ;;$", script, re.M), "ci and full must install the Core tools"


def test_no_workflow_chooses_a_core_tool_version() -> None:
    # A workflow that pins, upgrades or installs a Core tool by itself is a copy
    # that a Core bump would have to edit (or, unpinned, one that floats).
    pattern = re.compile(
        r"pip\s+install\b[^\n]*?(?:--upgrade\s+pip\b|\bpip==|\b(?:ruff|mypy|pytest|pre-commit)(?:[=<>~ ]|$))",
        re.M,
    )
    found = sorted(
        f"{workflow.name}: {match.group(0).strip()}"
        for workflow in (ROOT / ".github" / "workflows").glob("*.y*ml")
        for match in pattern.finditer(workflow.read_text(encoding="utf-8"))
    )
    assert not found, f"Core tools come from tools/deps/install_locked.sh: {found}"


def test_semgrep_comes_only_from_cores_installer() -> None:
    # Semgrep's version is Core's (toolchain-lock.json); a workflow that pulls a
    # Semgrep image, action or package by itself floats or copies it.
    text = "\n".join(w.read_text(encoding="utf-8") for w in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")))
    forbidden = re.findall(r"(?:returntocorp|semgrep)/semgrep[\w-]*(?:@|:)\S+|pip\s+install\b[^\n]*\bsemgrep\b", text)
    assert not forbidden, f"Semgrep must come from install-consumer-ci@v2: {forbidden}"
    if "semgrep" in text:
        assert "Quantum-L9/l9-ci-core/.github/actions/install-consumer-ci@v2" in text


def test_dependabot_is_the_org_render() -> None:
    # Quantum-L9/.github renders and restores this file (ADR-0019); a hand edit
    # here is replaced on the next seed, so a policy change belongs there.
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert text.startswith("# Rendered by Quantum-L9/.github ops/render-dependabot.js"), text[:120]


def test_dependabot_opens_only_safe_prs() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    for update in config["updates"]:
        ecosystem = update["package-ecosystem"]
        majors = [
            entry
            for entry in update.get("ignore", [])
            if entry.get("dependency-name") == "*" and entry.get("update-types") == ["version-update:semver-major"]
        ]
        assert majors, f"{ecosystem}: semver-major bumps must be ignored (safe-only)"
        groups = update.get("groups", {}).values()
        assert any("*" in g.get("patterns", []) and g.get("update-types") == ["minor", "patch"] for g in groups), (
            f"{ecosystem}: minor/patch bumps must land as one grouped PR"
        )


def test_dependabot_bumps_every_codeql_action_step_together() -> None:
    # A split bump fails CodeQL ("Loaded a configuration file for version X, but
    # running version Y"). The github-actions minor/patch group matches "*", so
    # init, analyze and upload-sarif always move in one PR; majors are ignored.
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    actions = next(u for u in config["updates"] if u["package-ecosystem"] == "github-actions")
    patterns = [p for group in actions.get("groups", {}).values() for p in group.get("patterns", [])]
    assert "*" in patterns or "github/codeql-action*" in patterns, patterns


def test_every_codeql_action_step_runs_one_version() -> None:
    refs = set()
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        refs.update(
            re.findall(r"uses:\s*github/codeql-action/[\w-]+@([0-9a-f]{40})", workflow.read_text(encoding="utf-8"))
        )
    assert len(refs) <= 1, f"github/codeql-action steps must share one SHA, found {sorted(refs)}"
