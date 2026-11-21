#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tooling, ci]
tags: [tooling, ci, dependencies, toolchain]
owner: engine-team
status: active
--- /L9_META ---

Fail closed unless CEG runs every Core-owned tool at Core's version.

Quantum-L9/l9-ci-core owns the shared tools: which ones
(.github/actions/install-consumer-ci/core-owned-tools.json) and their versions
(the lock that file names, toolchain-lock.json). Version drift from Core is
prohibited org-wide, so this script reads both from Core instead of keeping a
copy that could go stale. It compares against Core main (L9_CORE_TOOLCHAIN_REF
overrides it), the ref the org's required workflow runs: Core's lock there is
the reference, not the v2 installer tag.

Rules, per Core-owned tool:

* a tool Core ships on PyPI (its ``uv`` ecosystem) is declared nowhere in
  pyproject.toml, is in ``[tool.uv] exclude-dependencies``, is absent from
  uv.lock, is ignored by Dependabot (pip excepted: uv.lock never holds it), and
  runs in pre-commit only as a local system hook;
* a Core pre-commit repository that CEG does pin (gitleaks) sits on the commit
  Core's version tag names;
* tools/deps/uv-bootstrap.txt pins Core's uv once Core's lock names one.

The checks are pure functions over the repository files and Core's metadata
(tests/unit/test_check_core_toolchain.py). ``main`` fetches the metadata and
resolves tags over the network, so CI runs it in the networked, advisory
``Core Toolchain`` job (ci.yml), never in the unit suite. An unreachable Core
contract or tag fails closed: a toolchain that cannot be checked has not been.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CORE_REPOSITORY = "https://raw.githubusercontent.com/Quantum-L9/l9-ci-core"
CORE_TOOLS_PATH = ".github/actions/install-consumer-ci/core-owned-tools.json"
CORE_TOOLS_SCHEMA = "l9.core-owned-tools/v1"

TagResolver = Callable[[str, str], str]


def canonical(name: str) -> str:
    """PEP 503 normalised package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(spec: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return canonical(match.group(1)) if match else ""


def core_ref() -> str:
    # Unset or empty both mean Core main.
    return os.environ.get("L9_CORE_TOOLCHAIN_REF") or "main"


def python_tools(core_tools: dict[str, Any]) -> set[str]:
    """Core-owned tools a Python consumer could take from PyPI (the uv ecosystem)."""
    return {canonical(name) for tool in core_tools["tools"] for name in tool["ecosystems"].get("uv", [])}


def check_pyproject(pyproject: dict[str, Any], uv_lock: dict[str, Any], core: set[str]) -> list[str]:
    errors: list[str] = []
    project = pyproject.get("project", {})
    sections = {"project.dependencies": project.get("dependencies", [])}
    sections.update(
        {
            f"project.optional-dependencies.{name}": specs
            for name, specs in project.get("optional-dependencies", {}).items()
        }
    )
    sections.update(
        {f"dependency-groups.{name}": specs for name, specs in pyproject.get("dependency-groups", {}).items()}
    )
    for section, specs in sections.items():
        for spec in specs:
            if isinstance(spec, str) and requirement_name(spec) in core:
                errors.append(f"pyproject.toml {section} declares Core-owned {spec}")
    excluded = {canonical(n) for n in pyproject.get("tool", {}).get("uv", {}).get("exclude-dependencies", [])}
    errors.extend(
        f"pyproject.toml [tool.uv] exclude-dependencies is missing Core-owned {name}"
        for name in sorted(core - excluded)
    )
    errors.extend(
        f"uv.lock holds Core-owned {package['name']}=={package.get('version')}; re-run `uv lock`"
        for package in uv_lock.get("package", [])
        if canonical(package["name"]) in core
    )
    return errors


def same_repository(left: str, right: str) -> bool:
    """Whether two repository URLs name one repository (case, trailing / and .git ignored)."""

    def key(url: str) -> str:
        url = url.strip().lower().rstrip("/")
        return url.removesuffix(".git")

    return key(left) == key(right)


def check_pre_commit(
    config: dict[str, Any], core_tools: dict[str, Any], lock: dict[str, str], resolve_tag: TagResolver
) -> list[str]:
    errors: list[str] = []
    repos = config.get("repos", [])
    for tool in core_tools["tools"]:
        for url in tool["ecosystems"].get("pre-commit", []):
            for repo in (r for r in repos if same_repository(r.get("repo", ""), url)):
                if tool["ecosystems"].get("uv"):
                    # On PyPI too: run the installed Core tool, not a second pinned copy.
                    errors.append(f".pre-commit-config.yaml pins {url}; run {tool['name']} as a local system hook")
                elif tool["name"] not in lock:
                    errors.append(f".pre-commit-config.yaml pins {url}, but Core's lock names no {tool['name']}")
                else:
                    version = lock[tool["name"]]
                    expected = resolve_tag(url, version)
                    if repo.get("rev") != expected:
                        errors.append(
                            f".pre-commit-config.yaml {url} rev {repo.get('rev')} is not Core's "
                            f"{tool['name']} {version} ({expected})"
                        )
    local = {hook["id"]: hook for repo in repos if repo.get("repo") == "local" for hook in repo.get("hooks", [])}
    for hook_id in ("ruff", "ruff-format", "mypy"):
        if local.get(hook_id, {}).get("language") != "system":
            errors.append(f".pre-commit-config.yaml hook {hook_id} must be repo: local, language: system")
    return errors


def check_uv_bootstrap(text: str, lock: dict[str, str]) -> list[str]:
    version = lock.get("uv")
    if version is None or re.search(rf"^uv=={re.escape(version)}\s", text, re.M):
        return []
    return [f"tools/deps/uv-bootstrap.txt must pin Core's uv {version}"]


def check_dependabot(config: dict[str, Any], core: set[str]) -> list[str]:
    python = next((u for u in config.get("updates", []) if u.get("package-ecosystem") == "uv"), None)
    if python is None:
        return [".github/dependabot.yml has no uv update block"]
    # Only an unconditional ignore stops every proposal: `versions` or
    # `update-types` narrow it to some updates.
    ignored = {
        canonical(e["dependency-name"])
        for e in python.get("ignore", [])
        if "versions" not in e and "update-types" not in e
    }
    # pip is excluded from uv.lock, so Dependabot has no pip to propose.
    return [f".github/dependabot.yml does not ignore Core-owned {name}" for name in sorted(core - {"pip"} - ignored)]


def check_repository(
    root: Path, core_tools: dict[str, Any], lock: dict[str, str], resolve_tag: TagResolver
) -> list[str]:
    if core_tools.get("schema") != CORE_TOOLS_SCHEMA:
        return [f"Core tool list schema {core_tools.get('schema')!r} is not {CORE_TOOLS_SCHEMA}"]
    core = python_tools(core_tools)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    pre_commit = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    dependabot = yaml.safe_load((root / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    bootstrap = (root / "tools" / "deps" / "uv-bootstrap.txt").read_text(encoding="utf-8")
    return [
        *check_pyproject(pyproject, uv_lock, core),
        *check_pre_commit(pre_commit, core_tools, lock, resolve_tag),
        *check_uv_bootstrap(bootstrap, lock),
        *check_dependabot(dependabot, core),
    ]


def fetch_core(ref: str, path: str) -> dict[str, Any]:
    url = f"{CORE_REPOSITORY}/{ref}/{path}"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read().decode("utf-8"))


def resolve_remote_tag(repo: str, version: str) -> str:
    if not repo.startswith("https://"):
        msg = f"refusing non-https repository {repo}"
        raise ValueError(msg)
    git = shutil.which("git")
    if git is None:
        msg = "git is required to resolve a Core tool's tag"
        raise RuntimeError(msg)
    tag = f"refs/tags/v{version}"
    # Fixed git binary; the repository is an https URL from Core's tool list.
    out = subprocess.run(
        [git, "ls-remote", "--", repo, tag, f"{tag}^{{}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    refs = {ref: sha for sha, ref in (line.split("\t", 1) for line in out.splitlines() if line)}
    sha = refs.get(f"{tag}^{{}}") or refs.get(tag)
    if not sha:
        msg = f"{repo} has no tag v{version}"
        raise RuntimeError(msg)
    return sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail closed unless CEG runs Core's toolchain.")
    parser.add_argument("--ref", default=core_ref(), help="l9-ci-core ref (default: L9_CORE_TOOLCHAIN_REF or main)")
    args = parser.parse_args(argv)
    try:
        core_tools = fetch_core(args.ref, CORE_TOOLS_PATH)
        lock = fetch_core(args.ref, core_tools["lock"])
        errors = check_repository(ROOT, core_tools, lock, resolve_remote_tag)
    except Exception as exc:  # any failure to check is a failed check
        print(f"FAIL: Core toolchain at {args.ref} could not be checked: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        return 1
    tools = ", ".join(sorted(tool["name"] for tool in core_tools["tools"]))
    print(f"PASS: CEG runs Core's toolchain at l9-ci-core {args.ref} ({tools})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
