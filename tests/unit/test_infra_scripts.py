"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [infra, shell, scripts]
owner: platform
status: active
--- /L9_META ---

Regression tests for the tools/infra and tools/dev shell scripts (RU-143..RU-148,
RG-179..RG-184).

Every script is copied (or the line under test extracted) into tmp_path and run
with bash and, where it calls out, stub binaries on PATH. Nothing here touches
docker, the network or a real service, and nothing writes into the repository.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed (RG-179..184 require bash)"),
]

REPO = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "bash"
GIT = shutil.which("git")

ROOT_LINES = {
    "tools/infra/check_env.sh": "REPO_ROOT",
    "tools/dev/dev_up.sh": "REPO_ROOT",
    "tools/infra/precommit_smoke.sh": "PROJECT_ROOT",
}


def _run(
    cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    # Fixed argv (bash + a copied repo script or extracted line) inside tmp_path; no untrusted input.
    return subprocess.run(  # noqa: S603
        cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=60, check=False
    )


def _env(tmp_path: Path, stubs: Path | None = None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in extra}
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    if stubs is not None:
        env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_repo(root: Path) -> Path:
    if GIT is None:
        pytest.skip("git binary not available (RG-179 requires git)")
    root.mkdir(parents=True, exist_ok=True)
    _run([GIT, "init", "-q"], cwd=root)
    (root / "sub").mkdir()
    return root


def _copy_script(rel: str, root: Path) -> Path:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / rel, dest)
    return dest


def _root_probe(rel: str, root: Path) -> Path:
    """A probe at the script's own path holding only its root-resolution line(s)."""
    var = ROOT_LINES[rel]
    text = (REPO / rel).read_text(encoding="utf-8")
    lines = [m.group(0) for m in re.finditer(rf"^(?:SCRIPT_DIR|{var})=.*$", text, re.MULTILINE)]
    assert any(line.startswith(f"{var}=") for line in lines), rel
    probe = root / rel
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("\n".join(["set -euo pipefail", *lines, f'printf "%s" "${var}"']) + "\n", encoding="utf-8")
    return probe


# ── RU-143 / RG-179: repo-root fallback runs only when git fails ────────────


@pytest.mark.parametrize("rel", sorted(ROOT_LINES))
def test_root_is_one_line_equal_to_git_toplevel(tmp_path: Path, rel: str) -> None:
    repo = _git_repo(tmp_path / "repo")
    probe = _root_probe(rel, repo)

    proc = _run([BASH, str(probe)], cwd=repo / "sub", env=_env(tmp_path))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(repo.resolve())


@pytest.mark.parametrize("rel", sorted(ROOT_LINES))
def test_root_falls_back_to_script_location_outside_git(tmp_path: Path, rel: str) -> None:
    plain = tmp_path / "plain"
    probe = _root_probe(rel, plain)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run([BASH, str(probe)], cwd=elsewhere, env=_env(tmp_path))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(plain.resolve())


def test_check_env_fails_when_required_var_unset(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / ".env.required").write_text("MUST_BE_SET\n", encoding="utf-8")
    script = _copy_script("tools/infra/check_env.sh", repo)

    proc = _run([BASH, str(script)], cwd=repo / "sub", env=_env(tmp_path, MUST_BE_SET=None))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "MUST_BE_SET — MISSING" in proc.stdout


# ── RU-144 / RG-180: short secrets fully masked ─────────────────────────────


def _check_env_output(tmp_path: Path, value: str) -> str:
    repo = _git_repo(tmp_path / "repo")
    (repo / ".env.required").write_text("SECRET_UNDER_TEST\n", encoding="utf-8")
    script = _copy_script("tools/infra/check_env.sh", repo)
    proc = _run([BASH, str(script)], cwd=repo, env=_env(tmp_path, SECRET_UNDER_TEST=value))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "SECRET_UNDER_TEST =" in ln)
    return line.split("SECRET_UNDER_TEST = ", 1)[1]


@pytest.mark.parametrize("value", ["abc", "abcdef", "12345678"])
def test_short_secret_reveals_no_characters(tmp_path: Path, value: str) -> None:
    masked = _check_env_output(tmp_path, value)
    assert masked == "****"


def test_long_secret_reveals_only_a_fixed_small_prefix(tmp_path: Path) -> None:
    value = "sk-live-0123456789abcdef"
    masked = _check_env_output(tmp_path, value)
    assert masked == "sk****"
    assert value[-4:] not in masked
