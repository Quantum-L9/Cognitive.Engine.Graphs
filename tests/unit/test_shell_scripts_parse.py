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
    call_log = tmp_path / "gh.log"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "gh", f'echo "$*" >> "{call_log}"; case "$1" in repo) echo owner/repo;; esac; exit 0')
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"}

    proc = _run([BASH, "setup-repo-vars.sh", "vars.env"], cwd=tmp_path, env=env)

    assert "syntax error" not in proc.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert "variable set FOO_BAR --body baz" in calls
    assert not [c for c in calls if "COMMENT_ONLY" in c]


# ── RU-150 / RG-186: entrypoint banner reports the effective chassis default ─

_UVICORN_EXEC = """exec uvicorn chassis.entrypoint:create_app \\
    --factory \\
    --host 0.0.0.0 \\
    --port ${API_PORT:-8000} \\
    --log-level ${LOG_LEVEL:-info} \\
    --access-log \\
    --timeout-keep-alive 30 \\
    "$@"
"""


def test_entrypoint_banner_default_matches_chassis_default() -> None:
    from chassis.entrypoint import DEFAULT_CHASSIS

    text = (REPO / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
    match = re.search(r'^echo "Chassis: \$\{L9_CHASSIS:-(?P<default>[a-z]+)\}"$', text, re.MULTILINE)
    assert match is not None, "banner line not found"
    assert match.group("default") == DEFAULT_CHASSIS
    assert "legacy default" not in text


def test_entrypoint_uvicorn_invocation_unchanged() -> None:
    text = (REPO / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
    assert text.endswith(_UVICORN_EXEC)


@needs_bash
def test_entrypoint_banner_prints_sdk_when_unset(tmp_path: Path) -> None:
    assert BASH is not None
    text = (REPO / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
    banner = next(ln for ln in text.splitlines() if ln.startswith('echo "Chassis: '))
    env = {k: v for k, v in os.environ.items() if k != "L9_CHASSIS"}
    proc = _run([BASH, "-c", banner], cwd=tmp_path, env=env)
    assert proc.stdout == "Chassis: sdk\n"


# ── RU-149 / RG-185: seed / gds / dev scripts use the live ingress ──────────
#
# A stub curl records each call (URL + body) and answers with a fixed HTTP code,
# so the scripts run end to end without a server.

_CURL_STUB = r"""
url=""; body=""; want_code=0; to_null=0; fail=0
while [ $# -gt 0 ]; do
  case "$1" in
    -X|-H) shift ;;
    -f|-sf|-fs|-sSf|--fail) fail=1 ;;
    -o) to_null=1; shift ;;
    -w) want_code=1; shift ;;
    -d|--data|--data-binary)
      shift
      if [ "$1" = "@-" ]; then body="$(cat)"; elif [ "${1#@}" != "$1" ]; then body="$(cat "${1#@}")"; else body="$1"; fi ;;
    http*) url="$1" ;;
  esac
  shift
done
printf '%s\n%s\n' "$url" "$body" >> "$CURL_LOG"
if [ "$want_code" = 1 ]; then printf '%s' "$CURL_CODE"; fi
[ "$fail" = 1 ] && [ "$CURL_CODE" -ge 400 ] && exit 22
[ "$to_null" = 1 ] || printf '{"status": "ok"}'
"""


def _curl_project(tmp_path: Path, script: str, code: str) -> tuple[Path, dict[str, str], Path]:
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / script, proj / "scripts" / script)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "curl", _CURL_STUB)
    log = tmp_path / "curl.log"
    env = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        "CURL_LOG": str(log),
        "CURL_CODE": code,
    }
    env.pop("API_URL", None)
    return proj, env, log


def _calls(log: Path) -> list[tuple[str, dict]]:
    import json

    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [(lines[i], json.loads(lines[i + 1]) if lines[i + 1] else {}) for i in range(0, len(lines), 2)]


def _seed(proj: Path, entities: dict[str, str]) -> None:
    seeds = proj / "domains" / "acme" / "seeds"
    seeds.mkdir(parents=True)
    for name, body in entities.items():
        (seeds / f"{name}.json").write_text(body, encoding="utf-8")


@needs_bash
def test_seed_posts_sync_through_v1_execute(tmp_path: Path) -> None:
    assert BASH is not None
    proj, env, log = _curl_project(tmp_path, "scripts-seed.sh", "200")
    _seed(proj, {"suppliers": '[{"id": "s1"}]'})

    proc = _run([BASH, "scripts/scripts-seed.sh", "acme", "http://api.test"], cwd=proj, env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(log) == [
        (
            "http://api.test/v1/execute",
            {"action": "sync", "tenant": "acme", "payload": {"entity_type": "suppliers", "batch": [{"id": "s1"}]}},
        )
    ]


@needs_bash
def test_seed_exits_nonzero_when_an_entity_fails(tmp_path: Path) -> None:
    assert BASH is not None
    proj, env, log = _curl_project(tmp_path, "scripts-seed.sh", "500")
    _seed(proj, {"buyers": '[{"id": "b1"}]', "suppliers": '[{"id": "s1"}]'})

    proc = _run([BASH, "scripts/scripts-seed.sh", "acme", "http://api.test"], cwd=proj, env=env)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    # Every entity is still attempted; the failure is reported, not hidden.
    assert [url for url, _ in _calls(log)] == ["http://api.test/v1/execute"] * 2
    assert "2 of 2" in proc.stdout


@needs_bash
def test_seed_rejects_a_seed_file_that_is_not_a_json_array(tmp_path: Path) -> None:
    assert BASH is not None
    proj, env, log = _curl_project(tmp_path, "scripts-seed.sh", "200")
    _seed(proj, {"suppliers": '{"id": "s1"}'})

    proc = _run([BASH, "scripts/scripts-seed.sh", "acme", "http://api.test"], cwd=proj, env=env)

    assert proc.returncode == 1
    assert _calls(log) == []


@needs_bash
def test_gds_trigger_posts_admin_gds_trigger(tmp_path: Path) -> None:
    assert BASH is not None
    proj, env, log = _curl_project(tmp_path, "scripts-gds-trigger.sh", "200")
    env["API_URL"] = "http://api.test"

    proc = _run([BASH, "scripts/scripts-gds-trigger.sh", "acme", "louvain"], cwd=proj, env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(log) == [
        (
            "http://api.test/v1/execute",
            {
                "action": "admin",
                "tenant": "acme",
                "payload": {"subaction": "gds_trigger", "domain_id": "acme", "job_name": "louvain"},
            },
        )
    ]


@needs_bash
@pytest.mark.parametrize("args", [["acme"], ["acme", "all"]])
def test_gds_trigger_requires_an_explicit_job_name(tmp_path: Path, args: list[str]) -> None:
    """DEC-52: no admin subaction triggers every job, so there is no 'all' mode."""
    assert BASH is not None
    proj, env, log = _curl_project(tmp_path, "scripts-gds-trigger.sh", "200")

    proc = _run([BASH, "scripts/scripts-gds-trigger.sh", *args], cwd=proj, env=env)

    assert proc.returncode != 0
    assert _calls(log) == []


@needs_bash
def test_gds_trigger_propagates_http_failure(tmp_path: Path) -> None:
    assert BASH is not None
    proj, env, _log = _curl_project(tmp_path, "scripts-gds-trigger.sh", "500")

    proc = _run([BASH, "scripts/scripts-gds-trigger.sh", "acme", "louvain"], cwd=proj, env=env)

    assert proc.returncode != 0


def test_dev_script_launches_the_chassis_entrypoint_factory() -> None:
    text = (REPO / "scripts" / "scripts-dev.sh").read_text(encoding="utf-8")
    assert "engine.api.app" not in text
    launches = re.findall(r"uvicorn (\S+) (--factory)", text)
    assert launches == [("chassis.entrypoint:create_app", "--factory")] * 2
