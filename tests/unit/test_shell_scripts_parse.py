"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [scripts, shell, l9-meta]
owner: platform
status: active
--- /L9_META ---

Shell scripts parse and run their documented action (RU-142 / RG-178).

Ten template scripts had every ``#`` (and the whitespace around it) stripped:
the shebang became ``!/usr/bin/env bash``, banners and comments became
executable text, and ``setup-repo-vars.sh`` stopped parsing. Each script is
exercised from a temporary copy with stub binaries on PATH; nothing here
touches docker, the network or a real service.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tools import l9_meta_injector as inj

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")

TEMPLATE_SCRIPTS = [
    "scripts/scripts-build.sh",
    "scripts/scripts-health.sh",
    "scripts/scripts-setup.sh",
    "scripts/scripts-migrate.sh",
    "scripts/scripts-dev.sh",
    "scripts/audit.sh",
    "scripts/scripts-seed.sh",
    "scripts/scripts-gds-trigger.sh",
    "scripts/scripts-test.sh",
    ".github/scripts/setup-repo-vars.sh",
]

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed (RG-178 environment: requires bash)")


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # Fixed argv (bash + a copied repo script) inside tmp_path; no untrusted input.
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.parametrize("rel", TEMPLATE_SCRIPTS)
def test_shebang_is_first(rel: str) -> None:
    assert (REPO / rel).read_bytes()[:2] == b"#!"


@needs_bash
@pytest.mark.parametrize("rel", TEMPLATE_SCRIPTS)
def test_bash_parses(rel: str) -> None:
    assert BASH is not None
    proc = _run([BASH, "-n", str(REPO / rel)], cwd=REPO)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("rel", TEMPLATE_SCRIPTS)
def test_no_uncommented_meta_delimiter(rel: str) -> None:
    text = (REPO / rel).read_text(encoding="utf-8")
    assert not re.search(r"^--- /?L9_META ---", text, re.MULTILINE)


@pytest.mark.parametrize("rel", TEMPLATE_SCRIPTS)
def test_header_is_what_the_injector_emits(rel: str) -> None:
    """The injector reports SKIP: re-injecting the registered header changes nothing."""
    fm = next(f for f in inj.FILE_REGISTRY if f.path == rel)
    text = (REPO / rel).read_text(encoding="utf-8")
    assert inj.inject_meta(text, fm, "graph") == text


@needs_bash
def test_health_script_reaches_service_checks(tmp_path: Path) -> None:
    """Repro K/shell_repros.sh section 1: the script runs past its header to the Neo4j/Redis checks."""
    assert BASH is not None
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "scripts-health.sh", proj / "scripts" / "scripts-health.sh")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "curl", 'case "$*" in *-w*) printf 200;; esac; exit 0')
    _stub(stubs, "redis-cli", "echo PONG")
    _stub(stubs, "docker", "exit 0")
    _stub(stubs, "cypher-shell", "exit 0")
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"}

    proc = _run([BASH, "scripts/scripts-health.sh"], cwd=proj, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "command not found" not in proc.stderr
    assert "API (http://localhost:8000)... ✅ UP (200)" in proc.stdout
    assert "Neo4j (http://localhost:7474)... ✅ UP" in proc.stdout
    assert "Redis (localhost:6379)... ✅ UP" in proc.stdout


@needs_bash
def test_setup_repo_vars_skips_comments_and_sets_values(tmp_path: Path) -> None:
    """The restored comment-skip regex skips comment lines instead of every line."""
    assert BASH is not None
    shutil.copy(REPO / ".github" / "scripts" / "setup-repo-vars.sh", tmp_path / "setup-repo-vars.sh")
    (tmp_path / "vars.env").write_text("# COMMENT_ONLY=nope\n\nFOO_BAR=baz\n", encoding="utf-8")
    log = tmp_path / "gh.log"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "gh", f'echo "$*" >> "{log}"; case "$1" in repo) echo owner/repo;; esac; exit 0')
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"}

    proc = _run([BASH, "setup-repo-vars.sh", "vars.env"], cwd=tmp_path, env=env)

    assert "syntax error" not in proc.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "variable set FOO_BAR --body baz" in calls
    assert not [c for c in calls if "COMMENT_ONLY" in c]
