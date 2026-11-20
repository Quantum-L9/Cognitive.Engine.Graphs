"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, ci]
tags: [test, unit, ci, secrets-scan, coverage, dependency-review, quality-gate]
owner: engine-team
status: active
--- /L9_META ---

Witnesses that the CI controls which call an outside service report what
happened: a scan that did not run, a rejected upload and a skipped review are
never reported, or aggregated, as an executed control.

The GitGuardian witnesses drive the real entry point the workflow calls
(``tools/scanner_outcomes.py gitguardian-scan``) against a stand-in ``docker``
on PATH. No scan is sent to the vendor.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tools import scanner_outcomes as so

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "scanner_outcomes.py"
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_CASES = yaml.safe_load((ROOT / "tests" / "fixtures" / "ci" / "quality_gate_cases.yaml").read_text())["cases"]
IMAGE = "gitguardian/ggshield:v0.0.0@sha256:" + "0" * 64

pytestmark = pytest.mark.unit

GG = so.GitGuardianOutcome

# What `docker run` prints before every successful cold pull. The workflow this
# replaces matched "Unable to find image" and skipped the scan as an
# infrastructure failure, so every cold-pull run was reported green unscanned.
COLD_PULL = (
    "Unable to find image 'gitguardian/ggshield:v0.0.0' locally\n"
    "v0.0.0: Pulling from gitguardian/ggshield\n"
    "Status: Downloaded newer image for gitguardian/ggshield:v0.0.0\n"
)
CLEAN = COLD_PULL + "\nNo secrets have been found\n"
# ggshield masks the match; this is its report shape, not a credential.
INCIDENT = (
    COLD_PULL + "commit 0000000\n>>> Incident 1(Secrets detection): Generic High Entropy Secret\n"
    '  1 | +fixture_value = "****************"\n'
)
NOT_CI = COLD_PULL + "Error: `secret scan ci` should only be used in a CI environment.\n"
SCANNER_CRASH = COLD_PULL + "Error: Unable to get commit range. Please submit an issue\n"
NO_DAEMON = "docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\n"
PULL_DENIED = "Unable to find image 'gitguardian/ggshield:v0.0.0' locally\ndocker: manifest unknown.\n"
MISSING_SCOPE = COLD_PULL + "Error: Token is missing the required scope scan to perform this operation.\n"
SERVER_SKIP = COLD_PULL + "Error: GitGuardian server is not responding.\nSkipping ggshield checks.\n"


def _run_scan(
    tmp_path: Path, *, docker_rc: int | None, docker_out: str = "", key: str | None = "stand-in-key"
) -> tuple[int, dict[str, str], str, str, list[str]]:
    """Run the workflow's entry point with a stand-in docker. Returns rc, outputs, summary, stdout, docker argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "docker.argv"
    if docker_rc is not None:
        (tmp_path / "docker.out").write_text(docker_out)
        docker = bin_dir / "docker"
        docker.write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{argv_log}"\ncat "{tmp_path / "docker.out"}"\nexit {docker_rc}\n'
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin" if docker_rc is not None else str(bin_dir),
        "GGSHIELD_IMAGE": IMAGE,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "CI": "true",
        "GITHUB_ACTIONS": "true",
    }
    if key is not None:
        env["GITGUARDIAN_API_KEY"] = key
    completed = subprocess.run(  # noqa: S603 -- fixed repository-owned script and interpreter
        [sys.executable, str(TOOL), "gitguardian-scan"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output_file = tmp_path / "output"
    outputs = dict(line.split("=", 1) for line in output_file.read_text().splitlines()) if output_file.exists() else {}
    summary = (tmp_path / "summary").read_text() if (tmp_path / "summary").exists() else ""
    argv = argv_log.read_text().splitlines() if argv_log.exists() else []
    return completed.returncode, outputs, summary, completed.stdout + completed.stderr, argv


# ── T01: the five witnesses, through the entry point the workflow calls ──────


def test_witness_clean_scan(tmp_path: Path) -> None:
    rc, outputs, summary, stdout, argv = _run_scan(tmp_path, docker_rc=0, docker_out=CLEAN)
    assert rc == 0
    assert outputs["outcome"] == "SCANNED_CLEAN"
    assert outputs["executed"] == "true"
    assert "EXECUTED: scanned, no secret found" in summary
    assert "::warning::" not in stdout
    assert "::error::" not in stdout
    assert argv[-4:] == [IMAGE, "secret", "scan", "ci"]


def test_witness_missing_ci_context_fails_and_is_not_a_scan(tmp_path: Path) -> None:
    """The hosted failure on run 44655221257: ggshield refused to run and the job went green."""
    rc, outputs, summary, stdout, _ = _run_scan(tmp_path, docker_rc=2, docker_out=NOT_CI)
    assert rc == 2
    assert outputs["outcome"] == "SCANNER_FAILED"
    assert outputs["executed"] == "false"
    assert outputs["reason"].startswith("ci_context_missing")
    assert "NOT EXECUTED: the scanner started and failed (blocking)" in summary
    assert "::error::GitGuardian SCANNER_FAILED" in stdout


@pytest.mark.parametrize(
    ("docker_rc", "docker_out"),
    [(125, NO_DAEMON), (125, PULL_DENIED), (127, 'exec: "ggshield": executable file not found in $PATH\n')],
)
def test_witness_unavailable_scanner_is_not_executed(tmp_path: Path, docker_rc: int, docker_out: str) -> None:
    rc, outputs, summary, stdout, _ = _run_scan(tmp_path, docker_rc=docker_rc, docker_out=docker_out)
    assert rc == 0, "requiredness is unchanged: a container that cannot start does not fail the job"
    assert outputs["outcome"] == "CONTAINER_FAILED_TO_START"
    assert outputs["executed"] == "false"
    assert "NOT EXECUTED: advisory skip" in summary
    assert "this is not a clean scan" in summary
    assert "::warning::GitGuardian CONTAINER_FAILED_TO_START: NOT EXECUTED" in stdout


def test_witness_pull_succeeds_then_scanner_fails(tmp_path: Path) -> None:
    """A cold pull prints "Unable to find image"; that text must not turn a scanner failure into a skip."""
    rc, outputs, summary, stdout, _ = _run_scan(tmp_path, docker_rc=128, docker_out=SCANNER_CRASH)
    assert rc == 128
    assert outputs["outcome"] == "SCANNER_FAILED"
    assert outputs["executed"] == "false"
    assert "advisory skip" not in summary
    assert "::error::GitGuardian SCANNER_FAILED: ggshield exited 128" in stdout


def test_witness_synthetic_secret_detected(tmp_path: Path) -> None:
    rc, outputs, summary, stdout, _ = _run_scan(tmp_path, docker_rc=1, docker_out=INCIDENT)
    assert rc == 1
    assert outputs["outcome"] == "SECRET_DETECTED"
    assert outputs["executed"] == "true"
    assert "EXECUTED: secret detected (blocking)" in summary
    assert "::error::GitGuardian SECRET_DETECTED" in stdout


def test_key_absent_is_not_executed_and_docker_is_not_called(tmp_path: Path) -> None:
    rc, outputs, summary, stdout, argv = _run_scan(tmp_path, docker_rc=0, docker_out=CLEAN, key=None)
    assert rc == 0, "requiredness is unchanged: a context without the key does not fail the job"
    assert outputs["outcome"] == "KEY_ABSENT"
    assert outputs["executed"] == "false"
    assert argv == []
    assert "NOT EXECUTED: advisory skip" in summary
    assert "::warning::GitGuardian KEY_ABSENT: NOT EXECUTED" in stdout


def test_key_reaches_the_container_by_name_only(tmp_path: Path) -> None:
    _, _, summary, stdout, argv = _run_scan(tmp_path, docker_rc=0, docker_out=CLEAN, key="stand-in-key")
    assert "stand-in-key" not in " ".join(argv)
    assert "stand-in-key" not in summary + stdout
    assert argv[argv.index("GITGUARDIAN_API_KEY") - 1] == "-e"


def test_container_receives_the_ci_context_ggshield_requires(tmp_path: Path) -> None:
    """ggshield v1.53.0 refuses `secret scan ci` without CI and picks GitHub from GITHUB_ACTIONS."""
    argv = so.gitguardian_docker_argv(IMAGE, tmp_path)
    passed = {argv[index + 1] for index, value in enumerate(argv) if value == "-e"}
    assert {"CI", "GITHUB_ACTIONS", "GITHUB_SHA", "GITHUB_PULL_BASE_SHA", "GITHUB_DEFAULT_BRANCH"} <= passed
    assert all("=" not in name for name in passed), "variables are passed by name, never by value"


# ── T01: the classifier, every outcome distinct ──────────────────────────────


@pytest.mark.parametrize(
    ("key_present", "exit_code", "output", "outcome", "executed", "blocking"),
    [
        (True, 0, CLEAN, GG.SCANNED_CLEAN, True, False),
        (True, 0, COLD_PULL + "\nNo new secrets have been found\n", GG.SCANNED_CLEAN, True, False),
        (True, 1, INCIDENT, GG.SECRET_DETECTED, True, True),
        (False, None, "", GG.KEY_ABSENT, False, False),
        (True, 128, MISSING_SCOPE, GG.TOKEN_LACKS_SCOPE, False, False),
        (True, 125, NO_DAEMON, GG.CONTAINER_FAILED_TO_START, False, False),
        (True, 126, "", GG.CONTAINER_FAILED_TO_START, False, False),
        (True, 127, "", GG.CONTAINER_FAILED_TO_START, False, False),
        (True, None, "", GG.CONTAINER_FAILED_TO_START, False, False),
        (True, 2, NOT_CI, GG.SCANNER_FAILED, False, True),
        (True, 3, COLD_PULL + "Error: Invalid API key.\n", GG.SCANNER_FAILED, False, True),
        (True, 4, COLD_PULL + "Error: GitGuardian server is not responding.\n", GG.SCANNER_FAILED, False, True),
        (True, 128, SCANNER_CRASH, GG.SCANNER_FAILED, False, True),
        # Exit 0 is not a scan unless ggshield said it scanned.
        (True, 0, SERVER_SKIP, GG.SCANNER_FAILED, False, True),
        (True, 0, COLD_PULL, GG.SCANNER_FAILED, False, True),
        (True, 0, "", GG.SCANNER_FAILED, False, True),
    ],
)
def test_classify_gitguardian(  # noqa: PLR0917 -- one parametrized row per outcome
    key_present: bool,
    exit_code: int | None,
    output: str,
    outcome: so.GitGuardianOutcome,
    executed: bool,
    blocking: bool,
) -> None:
    result = so.classify_gitguardian(key_present=key_present, exit_code=exit_code, output=output)
    assert (result.outcome, result.executed, result.blocking) == (outcome, executed, blocking)
    assert result.reason


def test_every_gitguardian_outcome_has_exactly_one_policy() -> None:
    """Each outcome is clean, blocking, or an advisory skip. None is unlabelled and none is two things."""
    clean = {GG.SCANNED_CLEAN}
    assert clean | so.BLOCKING_GITGUARDIAN | so.ADVISORY_NOT_EXECUTED_GITGUARDIAN == set(GG)
    assert not clean & so.BLOCKING_GITGUARDIAN
    assert not so.BLOCKING_GITGUARDIAN & so.ADVISORY_NOT_EXECUTED_GITGUARDIAN
    assert not so.EXECUTED_GITGUARDIAN & so.ADVISORY_NOT_EXECUTED_GITGUARDIAN


# ── T07: the aggregate fixture ───────────────────────────────────────────────


@pytest.mark.parametrize("case", GATE_CASES, ids=[case["name"] for case in GATE_CASES])
def test_quality_gate_fixture(case: dict[str, object]) -> None:
    jobs = dict.fromkeys(so.QUALITY_GATE_JOBS, "success")
    jobs |= {so.SECRETS_JOB: str(case["secrets"]), "semgrep": str(case["other"])}
    verdict = so.quality_gate_verdict(jobs, str(case["native"]))
    assert verdict.passed is case["passed"]
    counted = {"executed": verdict.executed, "not_executed": verdict.not_executed, "failed": verdict.failed}
    for bucket, members in counted.items():
        assert (so.SECRETS_JOB in members) is (bucket == case["counted"])
    assert set(verdict.executed) | set(verdict.not_executed) | set(verdict.failed) == set(so.QUALITY_GATE_JOBS)


def test_quality_gate_fixture_covers_every_native_outcome() -> None:
    succeeded = [case for case in GATE_CASES if case["secrets"] == "success"]
    assert {outcome.value for outcome in GG} <= {case["native"] for case in succeeded}
    executed = {case["native"] for case in succeeded if case["counted"] == "executed"}
    assert executed == {"SCANNED_CLEAN"}, "only a clean scan may count the secrets job as executed"


def _run_tool(command: str, tmp_path: Path, **env: str) -> tuple[int, dict[str, str], str, str]:
    completed = subprocess.run(  # noqa: S603 -- fixed repository-owned script and interpreter
        [sys.executable, str(TOOL), command],
        env={"GITHUB_OUTPUT": str(tmp_path / "output"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), **env},
        text=True,
        capture_output=True,
        check=False,
    )
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())
    return completed.returncode, outputs, (tmp_path / "summary").read_text(), completed.stdout


def _gate_env(native: str, secrets: str = "success") -> dict[str, str]:
    return {
        "SEMGREP_RESULT": "success",
        "SECRETS_SCAN_RESULT": secrets,
        "SECRETS_SCAN_NATIVE": native,
        "YAML_VALIDATE_RESULT": "success",
    }


def test_quality_gate_command_reports_a_skipped_scan_as_not_executed(tmp_path: Path) -> None:
    rc, outputs, summary, stdout = _run_tool("quality-gate", tmp_path, **_gate_env("KEY_ABSENT"))
    assert rc == 0, "requiredness is unchanged: an advisory skip does not fail the gate"
    assert outputs["not_executed"] == "secrets-scan"
    assert "secrets-scan" not in outputs["executed"]
    assert "2 of 3 controls executed, 1 not executed, 0 failed" in summary
    assert "NOT EXECUTED  secrets-scan: KEY_ABSENT" in stdout
    assert "::warning::secrets-scan was NOT EXECUTED and is not counted as a passed control" in stdout


def test_quality_gate_command_fails_on_success_without_a_native_result(tmp_path: Path) -> None:
    rc, outputs, _, stdout = _run_tool("quality-gate", tmp_path, **_gate_env(""))
    assert rc == 1
    assert outputs["failed"] == "secrets-scan"
    assert "::error::Quality gate FAILED" in stdout


# ── T08: dependency graph availability and review execution, separately ──────


@pytest.mark.parametrize(
    ("available", "http_status", "step_outcome", "graph", "review"),
    [
        ("true", "", "success", "AVAILABLE", "EXECUTED_PASSED"),
        ("true", "", "failure", "AVAILABLE", "EXECUTED_FAILED"),
        ("false", "404", "skipped", "UNAVAILABLE", "NOT_EXECUTED"),
        ("false", "403", "skipped", "UNAVAILABLE", "NOT_EXECUTED"),
        # Run 44772844698: the compare API answered 502 and the job reported
        # "enable Dependency graph" and went green. A failed check is not a known graph state.
        ("false", "502", "skipped", "UNKNOWN", "NOT_EXECUTED"),
        ("false", "", "skipped", "UNKNOWN", "NOT_EXECUTED"),
        ("true", "", "skipped", "AVAILABLE", "NOT_EXECUTED"),
        ("true", "", "cancelled", "AVAILABLE", "NOT_EXECUTED"),
        ("", "", "", "UNKNOWN", "NOT_EXECUTED"),
    ],
)
def test_dependency_review_reports_two_facts(  # noqa: PLR0917 -- one parametrized row per case
    tmp_path: Path, available: str, http_status: str, step_outcome: str, graph: str, review: str
) -> None:
    rc, outputs, summary, stdout = _run_tool(
        "dependency-review",
        tmp_path,
        GRAPH_AVAILABLE=available,
        GRAPH_HTTP_STATUS=http_status,
        REVIEW_STEP_OUTCOME=step_outcome,
    )
    assert rc == 0, "requiredness is unchanged: the report never fails the job"
    assert outputs == {"graph": graph, "review": review}
    assert f"dependency graph: `{graph}`" in summary
    assert f"review: `{review}`" in summary
    not_executed = review == "NOT_EXECUTED"
    assert ("No dependency was reviewed" in summary) is not_executed
    assert ("::warning::Dependency review NOT EXECUTED" in stdout) is not_executed


def test_dependency_graph_check_does_not_blame_settings_for_a_server_error() -> None:
    text = _workflow("supply-chain.yml")
    assert "enable Dependency graph in repo Security settings" not in text
    assert 'echo "status=${STATUS}" >> "$GITHUB_OUTPUT"' in text


# ── The workflows are bound to the classifier ────────────────────────────────


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def test_secrets_scan_has_no_inline_skip_branch() -> None:
    """The three warn-and-exit-0 branches lived in the workflow, where no test could run them."""
    text = _workflow("ci-quality.yml")
    job = text[text.index("  secrets-scan:") : text.index("  yaml-validate:")]
    assert "python3 tools/scanner_outcomes.py gitguardian-scan" in job
    assert "exit 0" not in job
    assert "skipping" not in job.lower()
    assert "native_outcome: ${{ steps.scan.outputs.outcome }}" in job
    assert "@sha256:" in job, "the scanner image stays digest-pinned in the workflow"


def test_quality_gate_reads_the_native_outcome_not_only_the_job_conclusion() -> None:
    text = _workflow("ci-quality.yml")
    gate = text[text.index("  quality-gate:") :]
    assert "SECRETS_SCAN_NATIVE: ${{ needs['secrets-scan'].outputs.native_outcome }}" in gate
    assert "SECRETS_SCAN_RESULT: ${{ needs['secrets-scan'].result }}" in gate
    assert "python3 tools/scanner_outcomes.py quality-gate" in gate
    assert "if: always()" in gate
    for job in so.QUALITY_GATE_JOBS:
        assert f"      - {job}\n" in gate, f"{job} left the gate's needs"


def test_ci_quality_has_no_coverage_upload() -> None:
    """T02 rebind: #514 removed the upload from this workflow. Nothing here may report coverage."""
    assert "codecov/codecov-action" not in _workflow("ci-quality.yml")
    assert "codecov/codecov-action" not in _workflow("deep-plan.yml")


def test_no_workflow_uploads_to_codecov() -> None:
    """T42: Codecov is retired. No workflow uploads to it, and none reports an upload outcome."""
    texts = {path.name: path.read_text() for path in sorted(WORKFLOWS.glob("*.yml"))}
    assert [name for name, text in texts.items() if "codecov/codecov-action" in text] == []
    assert [name for name, text in texts.items() if "scanner_outcomes.py coverage-upload" in text] == []


def test_dependency_review_reports_even_when_the_review_is_skipped() -> None:
    text = _workflow("supply-chain.yml")
    job = text[text.index("  dependency-review:") : text.index("  sbom:")]
    report = job[job.index("      - name: Report dependency review outcome") :]
    assert "if: always()" in report
    assert "GRAPH_AVAILABLE: ${{ steps.depgraph.outputs.available }}" in report
    assert "GRAPH_HTTP_STATUS: ${{ steps.depgraph.outputs.status }}" in report
    assert "REVIEW_STEP_OUTCOME: ${{ steps.review.outcome }}" in report
    assert "python3 tools/scanner_outcomes.py dependency-review" in report
    assert "review: ${{ steps.report.outputs.review }}" in job
    assert "graph: ${{ steps.report.outputs.graph }}" in job
