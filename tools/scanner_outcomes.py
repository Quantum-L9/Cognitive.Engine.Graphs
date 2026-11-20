"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tools, ci]
tags: [ci, secrets-scan, coverage, dependency-review, quality-gate]
owner: engine-team
status: active
--- /L9_META ---

Native outcome classification for the CI controls that call an outside service.

A job conclusion says whether a job's steps exited zero. It does not say that
GitGuardian scanned, or that GitHub reviewed dependencies. This module names what each control actually did, so a workflow
can report it and the Quality Gate cannot count a control that did not run.

It changes no policy: which outcomes fail a job is what the workflows already
did, written down once (``BLOCKING_GITGUARDIAN``,
``ADVISORY_NOT_EXECUTED_GITGUARDIAN``). Standard library only, because the
workflow steps run it with the runner's python3 before anything is installed.
Each subcommand reads its inputs from the environment.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# ── GitGuardian ──────────────────────────────────────────────────────────────


class GitGuardianOutcome(StrEnum):
    SCANNED_CLEAN = "SCANNED_CLEAN"
    SECRET_DETECTED = "SECRET_DETECTED"  # noqa: S105 -- an outcome name, not a credential
    KEY_ABSENT = "KEY_ABSENT"
    TOKEN_LACKS_SCOPE = "TOKEN_LACKS_SCOPE"  # noqa: S105 -- an outcome name, not a credential
    CONTAINER_FAILED_TO_START = "CONTAINER_FAILED_TO_START"
    SCANNER_FAILED = "SCANNER_FAILED"


# The scan ran to a verdict.
EXECUTED_GITGUARDIAN = frozenset({GitGuardianOutcome.SCANNED_CLEAN, GitGuardianOutcome.SECRET_DETECTED})
# Policy carried over unchanged from the workflow this replaces: these fail the job.
BLOCKING_GITGUARDIAN = frozenset({GitGuardianOutcome.SECRET_DETECTED, GitGuardianOutcome.SCANNER_FAILED})
# Policy carried over unchanged: these did not scan and do not fail the job.
# They are reported as NOT EXECUTED and are never counted as an executed control.
ADVISORY_NOT_EXECUTED_GITGUARDIAN = frozenset(
    {
        GitGuardianOutcome.KEY_ABSENT,
        GitGuardianOutcome.TOKEN_LACKS_SCOPE,
        GitGuardianOutcome.CONTAINER_FAILED_TO_START,
    }
)

# `docker run` reserves these for its own failures: 125 the daemon or the image
# pull failed, 126 the command could not be invoked, 127 it was not found.
# ggshield exits 0, 1, 2, 3, 4 or 128 (ggshield/core/errors.py ExitCode, v1.53.0).
_DOCKER_RUN_FAILURES = {
    125: "docker could not create the container (daemon unavailable or image pull failed)",
    126: "the container command could not be invoked",
    127: "the container command was not found",
}
_GGSHIELD_SECRETS_FOUND = 1
_SCOPE_MARKER = "missing the required scope"
_CI_CONTEXT_MARKER = "should only be used in a ci environment"
_SERVER_SKIP_MARKER = "skipping ggshield checks"
_CLEAN_MARKERS = ("no secrets have been found", "no new secrets have been found")

# What ggshield needs to recognise GitHub Actions from inside the container
# (ggshield/cmd/secret/scan/ci.py and core/git_hooks/ci/, v1.53.0): `CI` or it
# refuses to run, `GITHUB_ACTIONS` to pick the provider, `GITHUB_SHA` and the
# range variables to pick the commits.
GGSHIELD_CI_ENV = (
    "CI",
    "GITHUB_ACTIONS",
    "GITHUB_SHA",
    "GITHUB_PUSH_BEFORE_SHA",
    "GITHUB_PUSH_BASE_SHA",
    "GITHUB_PULL_BASE_SHA",
    "GITHUB_DEFAULT_BRANCH",
)


@dataclass(frozen=True)
class GitGuardianResult:
    outcome: GitGuardianOutcome
    reason: str

    @property
    def executed(self) -> bool:
        return self.outcome in EXECUTED_GITGUARDIAN

    @property
    def blocking(self) -> bool:
        return self.outcome in BLOCKING_GITGUARDIAN


def classify_gitguardian(*, key_present: bool, exit_code: int | None, output: str) -> GitGuardianResult:
    """Name what the scan did from the key, the `docker run` exit code and its output.

    The exit code decides. Output text only refines an exit code that is
    already known, because `docker run` prints "Unable to find image ...
    locally" before every successful cold pull.
    """
    if not key_present:
        return GitGuardianResult(GitGuardianOutcome.KEY_ABSENT, "GITGUARDIAN_API_KEY is not set in this context")
    if exit_code is None:
        return GitGuardianResult(GitGuardianOutcome.CONTAINER_FAILED_TO_START, "docker is not available on this runner")
    if exit_code in _DOCKER_RUN_FAILURES:
        return GitGuardianResult(GitGuardianOutcome.CONTAINER_FAILED_TO_START, _DOCKER_RUN_FAILURES[exit_code])

    text = output.lower()
    if exit_code == 0:
        if _SERVER_SKIP_MARKER in text:
            return GitGuardianResult(
                GitGuardianOutcome.SCANNER_FAILED, "ggshield exited 0 after skipping its checks (server unavailable)"
            )
        if any(marker in text for marker in _CLEAN_MARKERS):
            return GitGuardianResult(GitGuardianOutcome.SCANNED_CLEAN, "ggshield scanned the commit range")
        return GitGuardianResult(GitGuardianOutcome.SCANNER_FAILED, "ggshield exited 0 without reporting a scan result")
    if _SCOPE_MARKER in text:
        return GitGuardianResult(GitGuardianOutcome.TOKEN_LACKS_SCOPE, "the API token is missing a required scope")
    if exit_code == _GGSHIELD_SECRETS_FOUND:
        return GitGuardianResult(GitGuardianOutcome.SECRET_DETECTED, "ggshield reported at least one incident")
    if _CI_CONTEXT_MARKER in text:
        return GitGuardianResult(
            GitGuardianOutcome.SCANNER_FAILED, "ci_context_missing: ggshield did not see a CI environment"
        )
    return GitGuardianResult(GitGuardianOutcome.SCANNER_FAILED, f"ggshield exited {exit_code}")


def gitguardian_docker_argv(image: str, workdir: Path) -> list[str]:
    """The `docker run` command: the pinned image, the key and the CI context by name only."""
    argv = ["docker", "run", "--rm", "--entrypoint", "ggshield", "-e", "GITGUARDIAN_API_KEY"]
    for name in GGSHIELD_CI_ENV:
        argv += ["-e", name]
    argv += ["-v", f"{workdir}:/src", "-w", "/src", image, "secret", "scan", "ci"]
    return argv


def gitguardian_exit_code(result: GitGuardianResult, scanner_exit_code: int | None) -> int:
    return (scanner_exit_code or 1) if result.blocking else 0


def gitguardian_report(result: GitGuardianResult) -> list[str]:
    verdict = {
        GitGuardianOutcome.SCANNED_CLEAN: "EXECUTED: scanned, no secret found",
        GitGuardianOutcome.SECRET_DETECTED: "EXECUTED: secret detected (blocking)",
        GitGuardianOutcome.SCANNER_FAILED: "NOT EXECUTED: the scanner started and failed (blocking)",
    }.get(result.outcome, "NOT EXECUTED: advisory skip under the existing policy; this is not a clean scan")
    return [
        "### GitGuardian secrets scan",
        "",
        f"- outcome: `{result.outcome.value}`",
        f"- executed: `{str(result.executed).lower()}`",
        f"- reason: {result.reason}",
        f"- {verdict}",
    ]


def run_gitguardian_scan() -> int:
    image = os.environ.get("GGSHIELD_IMAGE", "")
    if not image:
        _annotate("error", "GGSHIELD_IMAGE is not set; the workflow must name the pinned image")
        return 2
    key_present = bool(os.environ.get("GITGUARDIAN_API_KEY"))
    exit_code: int | None = None
    output = ""
    if key_present and shutil.which("docker"):
        completed = subprocess.run(
            gitguardian_docker_argv(image, Path.cwd()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        exit_code, output = completed.returncode, completed.stdout
        sys.stdout.write(output if output.endswith("\n") or not output else output + "\n")

    result = classify_gitguardian(key_present=key_present, exit_code=exit_code, output=output)
    _write_outputs({"outcome": result.outcome.value, "executed": str(result.executed).lower(), "reason": result.reason})
    _write_summary(gitguardian_report(result))
    title = f"GitGuardian {result.outcome.value}"
    if result.blocking:
        _annotate("error", f"{title}: {result.reason}")
    elif not result.executed:
        _annotate("warning", f"{title}: NOT EXECUTED, not a clean scan. {result.reason}")
    else:
        sys.stdout.write(f"{title}: {result.reason}\n")
    return gitguardian_exit_code(result, exit_code)


# ── Quality Gate aggregate ───────────────────────────────────────────────────

SECRETS_JOB = "secrets-scan"
QUALITY_GATE_JOBS = ("lint-format", "semgrep", SECRETS_JOB, "yaml-validate")


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    executed: tuple[str, ...]
    not_executed: tuple[str, ...]
    failed: tuple[str, ...]
    lines: tuple[str, ...] = ()


def quality_gate_verdict(job_results: dict[str, str], secrets_native: str) -> GateVerdict:
    """Aggregate job conclusions without letting a conclusion stand in for a native result.

    A job that did not conclude `success` fails the gate, as before. The secrets
    job is also read through its native outcome: `success` counts as an executed
    control only when the scan ran clean. An advisory skip passes under the
    existing policy and is listed as not executed. `success` with a missing,
    unknown or blocking native outcome is a contradiction and fails.
    """
    executed: list[str] = []
    not_executed: list[str] = []
    failed: list[str] = []
    lines: list[str] = []

    for job in QUALITY_GATE_JOBS:
        conclusion = job_results.get(job, "")
        if conclusion != "success":
            failed.append(job)
            detail = f" (native outcome: {secrets_native or 'none reported'})" if job == SECRETS_JOB else ""
            lines.append(f"FAILED        {job}: job concluded '{conclusion or 'unknown'}'{detail}")
            continue
        if job != SECRETS_JOB:
            executed.append(job)
            lines.append(f"EXECUTED      {job}: success")
            continue
        try:
            native = GitGuardianOutcome(secrets_native)
        except ValueError:
            failed.append(job)
            lines.append(
                f"FAILED        {job}: job concluded 'success' with no recognised native outcome "
                f"('{secrets_native}'); it cannot be counted as an executed scan"
            )
            continue
        if native is GitGuardianOutcome.SCANNED_CLEAN:
            executed.append(job)
            lines.append(f"EXECUTED      {job}: {native.value}")
        elif native in ADVISORY_NOT_EXECUTED_GITGUARDIAN:
            not_executed.append(job)
            lines.append(f"NOT EXECUTED  {job}: {native.value} (advisory skip under the existing policy)")
        else:
            failed.append(job)
            lines.append(f"FAILED        {job}: job concluded 'success' but the native outcome is {native.value}")

    return GateVerdict(not failed, tuple(executed), tuple(not_executed), tuple(failed), tuple(lines))


def run_quality_gate() -> int:
    # lint-format -> LINT_FORMAT_RESULT, and so on: one variable per job the gate needs.
    job_results = {job: os.environ.get(f"{job.upper().replace('-', '_')}_RESULT", "") for job in QUALITY_GATE_JOBS}
    verdict = quality_gate_verdict(job_results, os.environ.get("SECRETS_SCAN_NATIVE", ""))
    total = len(QUALITY_GATE_JOBS)
    headline = (
        f"Quality gate {'passed' if verdict.passed else 'FAILED'}: "
        f"{len(verdict.executed)} of {total} controls executed, "
        f"{len(verdict.not_executed)} not executed, {len(verdict.failed)} failed."
    )
    footer = "This gate did not run pytest. The full catalog is deep-plan.yml."
    sys.stdout.write("\n".join(["QUALITY GATE SUMMARY", "", *verdict.lines, "", headline, footer]) + "\n")
    _write_summary(["### Quality Gate", "", "```", *verdict.lines, "```", "", headline, "", footer])
    buckets = {"executed": verdict.executed, "not_executed": verdict.not_executed, "failed": verdict.failed}
    _write_outputs({name: ",".join(jobs) for name, jobs in buckets.items()})
    for job in verdict.not_executed:
        _annotate("warning", f"{job} was NOT EXECUTED and is not counted as a passed control")
    if not verdict.passed:
        _annotate("error", headline)
        return 1
    return 0


# ── Dependency review ────────────────────────────────────────────────────────


class GraphAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ReviewExecution(StrEnum):
    EXECUTED_PASSED = "EXECUTED_PASSED"
    EXECUTED_FAILED = "EXECUTED_FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"


# The compare API answers 404 or 403 when the dependency graph is not enabled
# or not readable with this token. Any other failure says nothing about the graph.
_GRAPH_NOT_READABLE_STATUS = frozenset({"403", "404"})


def classify_dependency_review(
    graph_available: str, review_step_outcome: str, http_status: str = ""
) -> tuple[GraphAvailability, ReviewExecution]:
    """Two separate facts: could the dependency graph be read, and did the review run."""
    if graph_available == "true":
        graph = GraphAvailability.AVAILABLE
    elif graph_available == "false" and http_status in _GRAPH_NOT_READABLE_STATUS:
        graph = GraphAvailability.UNAVAILABLE
    else:
        graph = GraphAvailability.UNKNOWN
    review = {"success": ReviewExecution.EXECUTED_PASSED, "failure": ReviewExecution.EXECUTED_FAILED}.get(
        review_step_outcome, ReviewExecution.NOT_EXECUTED
    )
    return graph, review


def run_dependency_review_report() -> int:
    http_status = os.environ.get("GRAPH_HTTP_STATUS", "")
    graph, review = classify_dependency_review(
        os.environ.get("GRAPH_AVAILABLE", ""), os.environ.get("REVIEW_STEP_OUTCOME", ""), http_status
    )
    graph_detail = {
        GraphAvailability.AVAILABLE: "the compare API answered",
        GraphAvailability.UNAVAILABLE: f"the compare API answered HTTP {http_status}: not enabled or not readable",
        GraphAvailability.UNKNOWN: (
            f"the availability check failed (HTTP {http_status or 'unknown'}); the graph state is not known"
        ),
    }[graph]
    lines = ["### Dependency review", "", f"- dependency graph: `{graph.value}` ({graph_detail})"]
    lines.append(f"- review: `{review.value}`")
    if review is ReviewExecution.NOT_EXECUTED:
        lines.append("- No dependency was reviewed. This run says nothing about vulnerabilities or licences.")
        _annotate(
            "warning",
            f"Dependency review NOT EXECUTED (dependency graph {graph.value}: {graph_detail}); "
            "this is not a clean review",
        )
    _write_summary(lines)
    _write_outputs({"graph": graph.value, "review": review.value})
    sys.stdout.write(f"dependency graph: {graph.value} ({graph_detail})\nreview: {review.value}\n")
    return 0


# ── Coverage upload ──────────────────────────────────────────────────────────


# ── GitHub Actions plumbing ──────────────────────────────────────────────────


def _append(env_name: str, lines: list[str]) -> None:
    target = os.environ.get(env_name)
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _write_outputs(values: dict[str, str]) -> None:
    _append("GITHUB_OUTPUT", [f"{name}={value}" for name, value in values.items()])


def _write_summary(lines: list[str]) -> None:
    _append("GITHUB_STEP_SUMMARY", [*lines, ""])


def _annotate(level: str, message: str) -> None:
    sys.stdout.write(f"::{level}::{message}\n")


_COMMANDS = {
    "gitguardian-scan": run_gitguardian_scan,
    "quality-gate": run_quality_gate,
    "dependency-review": run_dependency_review_report,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("command", choices=sorted(_COMMANDS))
    return _COMMANDS[parser.parse_args(argv).command]()


if __name__ == "__main__":
    raise SystemExit(main())
