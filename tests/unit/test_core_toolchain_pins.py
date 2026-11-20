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
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tomllib
import urllib.request
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

# The Core contract tools/deps/install_locked.sh installs from: the same
# repository, ref (L9_CI_CORE_REF, default v2) and directory.
CORE_REF = os.environ.get("L9_CI_CORE_REF", "v2")
_CORE_BASE = f"https://raw.githubusercontent.com/Quantum-L9/l9-ci-core/{CORE_REF}/"
_CORE_TOOLS_PATH = ".github/actions/install-consumer-ci/core-owned-tools.json"

_INSTALLER_PINS = (
    "https://raw.githubusercontent.com/Quantum-L9/l9-ci-core/${CORE_REF}/"
    ".github/actions/install-consumer-ci/requirements-consumer-ci.txt"
)


def _fetch_core(path: str) -> dict:
    # Fixed https URL to Core's contract. Unreachable fails the test rather
    # than falling back to a local copy that could go stale.
    with urllib.request.urlopen(_CORE_BASE + path, timeout=60) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


@functools.cache
def _core_owned_tools() -> dict:
    data = _fetch_core(_CORE_TOOLS_PATH)
    assert data.get("schema") == "l9.core-owned-tools/v1", data.get("schema")
    return data


@functools.cache
def _core_lock() -> dict:
    return _fetch_core(_core_owned_tools()["lock"])


def _core_python_tools() -> set[str]:
    """Core-owned tools a Python consumer could take from PyPI (the uv ecosystem)."""
    return {
        canonicalize_name(name) for tool in _core_owned_tools()["tools"] for name in tool["ecosystems"].get("uv", [])
    }


def test_core_tool_list_is_read_from_core() -> None:
    # Guards the reader: the six tools Core has owned since v2 was cut.
    assert {"mypy", "pip", "pre-commit", "pytest", "ruff", "semgrep"} <= _core_python_tools()


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_declares_no_core_owned_tool() -> None:
    pyproject = _pyproject()
    sections = {"project.dependencies": pyproject.get("project", {}).get("dependencies", [])}
    sections.update(
        {f"dependency-groups.{name}": specs for name, specs in pyproject.get("dependency-groups", {}).items()}
    )
    declared = sorted(
        f"{section}: {spec}"
        for section, specs in sections.items()
        for spec in specs
        if isinstance(spec, str) and canonicalize_name(Requirement(spec).name) in _core_python_tools()
    )
    assert not declared, f"Core-owned tools come from Core's installer, not pyproject.toml: {declared}"


def test_uv_lock_excludes_every_core_owned_tool() -> None:
    core = _core_python_tools()
    excluded = {canonicalize_name(name) for name in _pyproject()["tool"]["uv"].get("exclude-dependencies", [])}
    assert core <= excluded, f"[tool.uv] exclude-dependencies is missing {core - excluded}"
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = sorted(
        f"{package['name']}=={package.get('version')}"
        for package in lock.get("package", [])
        if canonicalize_name(package["name"]) in core
    )
    assert not locked, f"uv.lock holds a Core-owned tool; re-run `uv lock`: {locked}"


def _peeled_tag(repo: str, version: str) -> str:
    git = shutil.which("git")
    assert git, "git is required to resolve a Core tool's tag"
    tag = f"refs/tags/v{version}"
    out = subprocess.run(  # noqa: S603 - fixed git binary, fixed arguments
        [git, "ls-remote", repo, tag, f"{tag}^{{}}"], check=True, capture_output=True, text=True, timeout=60
    ).stdout
    refs = {ref: sha for sha, ref in (line.split("\t", 1) for line in out.splitlines() if line)}
    assert refs, f"{repo} has no tag v{version}"
    return refs.get(f"{tag}^{{}}", refs.get(tag, ""))


def test_pre_commit_runs_core_tools_at_cores_version() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    lock = _core_lock()
    for tool in _core_owned_tools()["tools"]:
        for url in tool["ecosystems"].get("pre-commit", []):
            for repo in (r for r in config.get("repos", []) if r.get("repo") == url):
                # A tool Core also ships on PyPI runs as the installed Core tool
                # (repo: local, language: system), never a second pinned copy.
                assert not tool["ecosystems"].get("uv"), f"{tool['name']}: use a local system hook, not {url}"
                assert tool["name"] in lock, f"{tool['name']}: Core's lock names no version"
                assert repo["rev"] == _peeled_tag(url, lock[tool["name"]]), (
                    f"{url} must run Core's {tool['name']} {lock[tool['name']]}, not {repo['rev']}"
                )
    entries = {hook["id"]: hook for repo in config["repos"] if repo["repo"] == "local" for hook in repo["hooks"]}
    for hook_id in ("ruff", "ruff-format", "mypy"):
        assert entries[hook_id]["language"] == "system", hook_id


def test_uv_bootstrap_installs_cores_uv() -> None:
    version = _core_lock().get("uv")
    if version is None:
        # Removal trigger: Core's lock at CORE_REF names uv (it does on Core main).
        pytest.skip(f"Core's lock at {CORE_REF} names no uv version yet")
    text = (ROOT / "tools" / "deps" / "uv-bootstrap.txt").read_text(encoding="utf-8")
    assert re.search(rf"^uv=={re.escape(version)} ", text, re.M), f"uv-bootstrap.txt must pin Core's uv {version}"


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


def test_dependabot_ignores_core_owned_tools() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    # uv when uv.lock exists (the org render's rule), which is the case here.
    python = next(u for u in config["updates"] if u["package-ecosystem"] == "uv")
    ignored = {entry["dependency-name"].lower() for entry in python.get("ignore", []) if "versions" not in entry}
    # pip joined Core's list after this render was seeded; it is excluded from
    # uv.lock, so Dependabot has no pip to propose in the meantime.
    expected = _core_python_tools() - {"pip"}
    assert expected <= ignored, f"Dependabot must not propose Core-owned tools: missing {expected - ignored}"


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
