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


# ── RU-145 / RG-181: build-context errors reach the exit status ─────────────

_DOCKER_CONFIG_STUB = """
for a in "$@"; do
  if [ "$a" = config ]; then
    f=""
    while [ $# -gt 0 ]; do if [ "$1" = -f ]; then f=$2; fi; shift; done
    cat "$f"
    exit 0
  fi
done
exit 0"""


def _docker_validate(tmp_path: Path, context: str) -> subprocess.CompletedProcess[str]:
    work = tmp_path / "dv"
    work.mkdir()
    (work / "docker-compose.yml").write_text(
        f"services:\n  api:\n    build:\n      context: {context}\n      dockerfile: Dockerfile\n", encoding="utf-8"
    )
    (work / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (work / "app").mkdir()
    shutil.copy(REPO / "tools" / "infra" / "docker_validate.sh", work / "docker_validate.sh")
    stubs = tmp_path / "stubs"
    _stub(stubs, "docker", _DOCKER_CONFIG_STUB)
    return _run([BASH, "./docker_validate.sh", "check-only"], cwd=work, env=_env(tmp_path, stubs))


def test_docker_validate_missing_build_context_fails(tmp_path: Path) -> None:
    proc = _docker_validate(tmp_path, "./does_not_exist")
    assert "Build context directory not found" in proc.stderr
    assert "ALL CHECKS PASSED" not in proc.stdout
    assert proc.returncode == 1


def test_docker_validate_existing_build_context_passes(tmp_path: Path) -> None:
    proc = _docker_validate(tmp_path, "./app")
    assert "Build context exists" in proc.stdout
    assert "ALL CHECKS PASSED" in proc.stdout
    assert proc.returncode == 0, proc.stderr


# ── RU-146 / RG-182: smoke verdict from pytest's exit status ────────────────


def _smoke_section(tmp_path: Path, pytest_output: str, pytest_rc: int) -> dict[str, int]:
    text = (REPO / "tools" / "infra" / "test_everything.sh").read_text(encoding="utf-8")
    helpers = re.findall(r"^test_(?:pass|fail|info|skip)\(\) \{.*\}$", text, re.MULTILINE)
    start = text.index("# 8. Automated Test Suite")
    end = text.index("# 9. WebSocket")
    log = tmp_path / "smoke_test_output.txt"
    section = text[start:end].replace("/tmp/smoke_test_output.txt", str(log))
    stubs = tmp_path / "stubs"
    out_file = tmp_path / "pytest_out.txt"
    out_file.write_text(pytest_output, encoding="utf-8")
    _stub(stubs, "docker", f'cat "{out_file}"; exit {pytest_rc}')
    snippet = "\n".join(
        [
            "set +e",
            "RED=; GREEN=; YELLOW=; NC=",
            "PASSED=0; FAILED=0",
            "API_CONTAINER=api; SMOKE_TEST_PATH=tests/",
            *helpers,
            section,
            'echo "COUNTS PASSED=$PASSED FAILED=$FAILED"',
        ]
    )
    proc = _run([BASH, "-c", snippet], cwd=tmp_path, env=_env(tmp_path, stubs))
    assert log.read_text(encoding="utf-8") == pytest_output  # output still captured to the smoke log
    counts = re.search(r"COUNTS PASSED=(\d+) FAILED=(\d+)", proc.stdout)
    assert counts is not None, proc.stdout + proc.stderr
    return {"passed": int(counts.group(1)), "failed": int(counts.group(2))}


def test_smoke_mixed_pass_fail_counts_as_failure(tmp_path: Path) -> None:
    out = "tests/test_a.py::test_x FAILED\n=== 3 failed, 10 passed in 1.2s ===\n"
    assert _smoke_section(tmp_path, out, 1) == {"passed": 0, "failed": 1}


def test_smoke_nonzero_exit_without_failed_lines_counts_as_failure(tmp_path: Path) -> None:
    out = "ERROR: file or directory not found: tests/\n=== no tests ran in 0.01s ===\n"
    assert _smoke_section(tmp_path, out, 4) == {"passed": 0, "failed": 1}


def test_smoke_all_passed_counts_as_pass(tmp_path: Path) -> None:
    out = "tests/test_a.py::test_x PASSED\n=== 13 passed in 1.2s ===\n"
    assert _smoke_section(tmp_path, out, 0) == {"passed": 1, "failed": 0}
