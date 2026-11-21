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

Offline tests of scripts/check_core_toolchain.py's rules.

The script reads Core's tool list and lock over the network; these tests feed
its pure checks fixed metadata and a fake tag resolver, so the unit suite never
touches the network. The metadata below is test input shaped like Core's, not a
copy of the policy: the networked run in ci.yml reads the real list.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("check_core_toolchain", _ROOT / "scripts" / "check_core_toolchain.py")
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("check_core_toolchain.py did not load")
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

GITLEAKS = "https://github.com/gitleaks/gitleaks"
RUFF_HOOKS = "https://github.com/astral-sh/ruff-pre-commit"
TAGS = {(GITLEAKS, "8.24.3"): "1" * 40}

CORE_TOOLS = {
    "schema": "l9.core-owned-tools/v1",
    "lock": ".github/actions/install-consumer-ci/toolchain-lock.json",
    "tools": [
        {"name": "bandit", "ecosystems": {"pip": ["bandit"], "uv": ["bandit"]}},
        {"name": "biome", "ecosystems": {"npm": ["@biomejs/biome"]}},
        {"name": "gitleaks", "ecosystems": {"pre-commit": [GITLEAKS]}},
        {"name": "pip", "ecosystems": {"pip": ["pip"], "uv": ["pip"]}},
        {"name": "pip-audit", "ecosystems": {"uv": ["pip-audit"]}},
        {"name": "ruff", "ecosystems": {"pre-commit": [RUFF_HOOKS], "uv": ["ruff"]}},
        {"name": "uv", "ecosystems": {"uv": ["uv"]}},
    ],
}
LOCK = {"bandit": "1.8.6", "gitleaks": "8.24.3", "pip-audit": "2.9.0", "ruff": "0.16.1", "uv": "0.13.8"}
LOCAL_HOOKS = {
    "repo": "local",
    "hooks": [{"id": hook_id, "language": "system"} for hook_id in ("ruff", "ruff-format", "mypy")],
}


def _resolve(repo: str, version: str) -> str:
    return TAGS[(repo, version)]


def _clean_pyproject() -> dict:
    return {
        "dependency-groups": {"ci": ["pyyaml>=6"]},
        "tool": {"uv": {"exclude-dependencies": ["bandit", "pip", "pip_audit", "ruff", "uv"]}},
    }


def test_python_tools_are_the_uv_ecosystem_names() -> None:
    assert _mod.python_tools(CORE_TOOLS) == {"bandit", "pip", "pip-audit", "ruff", "uv"}


def test_clean_pyproject_passes() -> None:
    assert _mod.check_pyproject(_clean_pyproject(), {"package": []}, _mod.python_tools(CORE_TOOLS)) == []


def test_declared_core_tool_fails() -> None:
    pyproject = _clean_pyproject()
    pyproject["dependency-groups"]["ci"].append("Pip_Audit==2.10.1")
    errors = _mod.check_pyproject(pyproject, {"package": []}, _mod.python_tools(CORE_TOOLS))
    assert errors == ["pyproject.toml dependency-groups.ci declares Core-owned Pip_Audit==2.10.1"]


def test_missing_exclusion_and_locked_core_tool_fail() -> None:
    pyproject = _clean_pyproject()
    pyproject["tool"]["uv"]["exclude-dependencies"].remove("bandit")
    lock = {"package": [{"name": "bandit", "version": "1.9.4"}]}
    errors = _mod.check_pyproject(pyproject, lock, _mod.python_tools(CORE_TOOLS))
    assert errors == [
        "pyproject.toml [tool.uv] exclude-dependencies is missing Core-owned bandit",
        "uv.lock holds Core-owned bandit==1.9.4; re-run `uv lock`",
    ]


def test_pre_commit_on_cores_tag_passes() -> None:
    config = {"repos": [{"repo": GITLEAKS, "rev": "1" * 40}, LOCAL_HOOKS]}
    assert _mod.check_pre_commit(config, CORE_TOOLS, LOCK, _resolve) == []


def test_pre_commit_off_cores_tag_fails() -> None:
    config = {"repos": [{"repo": GITLEAKS, "rev": "2" * 40}, LOCAL_HOOKS]}
    (error,) = _mod.check_pre_commit(config, CORE_TOOLS, LOCK, _resolve)
    assert "is not Core's gitleaks 8.24.3" in error


def test_pre_commit_pinning_a_pypi_core_tool_fails() -> None:
    config = {"repos": [{"repo": RUFF_HOOKS, "rev": "v0.16.1"}, LOCAL_HOOKS]}
    assert _mod.check_pre_commit(config, CORE_TOOLS, LOCK, _resolve) == [
        f".pre-commit-config.yaml pins {RUFF_HOOKS}; run ruff as a local system hook"
    ]


def test_pre_commit_core_hook_not_system_fails() -> None:
    hooks = {"repo": "local", "hooks": [{"id": "ruff", "language": "python"}]}
    errors = _mod.check_pre_commit({"repos": [hooks]}, CORE_TOOLS, LOCK, _resolve)
    assert ".pre-commit-config.yaml hook ruff must be repo: local, language: system" in errors


def test_uv_bootstrap_follows_cores_uv() -> None:
    assert _mod.check_uv_bootstrap("uv==0.13.8 \\\n    --hash=sha256:x\n", LOCK) == []
    assert _mod.check_uv_bootstrap("uv==0.13.7 \\\n", LOCK) == ["tools/deps/uv-bootstrap.txt must pin Core's uv 0.13.8"]
    # A Core lock that names no uv yet imposes no uv version.
    assert _mod.check_uv_bootstrap("uv==0.13.7 \\\n", {}) == []


def test_dependabot_must_ignore_core_tools_but_pip() -> None:
    ignore = [{"dependency-name": name} for name in ("bandit", "pip-audit", "ruff")]
    config = {"updates": [{"package-ecosystem": "uv", "ignore": ignore}]}
    assert _mod.check_dependabot(config, _mod.python_tools(CORE_TOOLS)) == [
        ".github/dependabot.yml does not ignore Core-owned uv"
    ]


def test_core_ref_defaults_to_core_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("L9_CORE_TOOLCHAIN_REF", raising=False)
    assert _mod.core_ref() == "main"
    monkeypatch.setenv("L9_CORE_TOOLCHAIN_REF", "")
    assert _mod.core_ref() == "main"
    monkeypatch.setenv("L9_CORE_TOOLCHAIN_REF", "v2")
    assert _mod.core_ref() == "v2"


def test_unknown_core_schema_fails_closed() -> None:
    errors = _mod.check_repository(_ROOT, {"schema": "other", "tools": []}, {}, _resolve)
    assert errors == ["Core tool list schema 'other' is not l9.core-owned-tools/v1"]


def test_optional_dependency_declaring_core_tool_fails() -> None:
    pyproject = _clean_pyproject()
    pyproject["project"] = {"optional-dependencies": {"audit": ["ruff==0.15.0"]}}
    errors = _mod.check_pyproject(pyproject, {"package": []}, _mod.python_tools(CORE_TOOLS))
    assert errors == ["pyproject.toml project.optional-dependencies.audit declares Core-owned ruff==0.15.0"]


def test_equivalent_repository_urls_still_match() -> None:
    for spelling in (RUFF_HOOKS + ".git", RUFF_HOOKS + "/", RUFF_HOOKS.upper()):
        config = {"repos": [{"repo": spelling, "rev": "v0.16.1"}, LOCAL_HOOKS]}
        assert _mod.check_pre_commit(config, CORE_TOOLS, LOCK, _resolve), spelling
    assert not _mod.same_repository(RUFF_HOOKS, GITLEAKS)


def test_partial_dependabot_ignore_does_not_count() -> None:
    ignore = [{"dependency-name": name} for name in ("bandit", "pip-audit", "uv")]
    ignore.append({"dependency-name": "ruff", "update-types": ["version-update:semver-major"]})
    config = {"updates": [{"package-ecosystem": "uv", "ignore": ignore}]}
    assert _mod.check_dependabot(config, _mod.python_tools(CORE_TOOLS)) == [
        ".github/dependabot.yml does not ignore Core-owned ruff"
    ]


def _ls_remote(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(_mod.subprocess, "run", run)
    return calls


def test_resolver_takes_a_lightweight_tags_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _ls_remote(monkeypatch, f"{'a' * 40}\trefs/tags/v8.24.3\n")
    assert _mod.resolve_remote_tag(GITLEAKS, "8.24.3") == "a" * 40
    assert calls[0][1:3] == ["ls-remote", "--"]


def test_resolver_peels_an_annotated_tag_to_its_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    _ls_remote(monkeypatch, f"{'b' * 40}\trefs/tags/v8.24.3\n{'c' * 40}\trefs/tags/v8.24.3^{{}}\n")
    assert _mod.resolve_remote_tag(GITLEAKS, "8.24.3") == "c" * 40


def test_resolver_fails_on_a_missing_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    _ls_remote(monkeypatch, "")
    with pytest.raises(RuntimeError, match="has no tag v9.9.9"):
        _mod.resolve_remote_tag(GITLEAKS, "9.9.9")


def test_resolver_refuses_a_non_https_repository() -> None:
    with pytest.raises(ValueError, match="non-https"):
        _mod.resolve_remote_tag("file:///tmp/repo", "1.0.0")


def test_main_fails_closed_when_core_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(ref: str, path: str) -> dict:
        raise OSError(f"cannot reach Core at {ref}")

    monkeypatch.setattr(_mod, "fetch_core", unreachable)
    assert _mod.main(["--ref", "v2"]) == 2


def test_main_fails_on_drift_and_passes_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {_mod.CORE_TOOLS_PATH: CORE_TOOLS, CORE_TOOLS["lock"]: LOCK}
    monkeypatch.setattr(_mod, "fetch_core", lambda ref, path: metadata[path])
    monkeypatch.setattr(_mod, "check_repository", lambda root, tools, lock, resolve: ["drift"])
    assert _mod.main(["--ref", "v2"]) == 1
    monkeypatch.setattr(_mod, "check_repository", lambda root, tools, lock, resolve: [])
    assert _mod.main(["--ref", "v2"]) == 0
