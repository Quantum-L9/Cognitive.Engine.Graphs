#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [audit]
# tags: [L9_TEMPLATE, audit, harness]
# owner: platform
# status: active
# --- /L9_META ---
"""
L9 Audit Harness — Single-Entrypoint Static Analysis Orchestrator
=================================================================

Coordinates all static analysis tools in the correct order and produces
a consolidated report. This is what `make audit` and CI should invoke.

Orchestration sequence:
    1. Architecture audit  (tools/audit_engine.py)
    2. Spec coverage       (tools/spec_extract.py)
    3. Contract wiring     (tools/verify_contracts.py)
    4. Baseline ratchet    (tools/audit_baseline.json, see "Baseline ratchet" below)
    5. Consolidated report (artifacts/harness_report.md)

Exit codes:
    0 — all clean (no CRITICAL/HIGH findings, contracts wired, no regression
        against the baseline)
    1 — CRITICAL or HIGH findings exist, contracts broken, or a count
        regressed against the baseline
    2 — harness infrastructure error (missing tools, bad YAML, a step that
        timed out, a missing or malformed baseline, etc.)

Usage:
    python tools/audit_harness.py                    # default (blocks on CRITICAL/HIGH)
    python tools/audit_harness.py --strict           # also blocks on MISSING spec features
    python tools/audit_harness.py --skip-contracts   # skip contract wiring check
    python tools/audit_harness.py --json             # output JSON summary to stdout
    python tools/audit_harness.py --update-baseline  # deliberate re-baseline (make harness-baseline)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONTRACT_WIRING_STEP = "Contract Wiring"
STEP_TIMEOUT_S = 120
INFRA_EXIT_CODE = 2
TIMEOUT_EXIT_CODE = 124
# Step exit codes that mean the step could not run to a verdict — an
# infrastructure error, never an audit finding.
INFRA_EXIT_CODES = frozenset({INFRA_EXIT_CODE, TIMEOUT_EXIT_CODE})


@dataclass
class StepResult:
    """Result from a single audit step."""

    name: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    passed: bool = True
    skipped: bool = False


@dataclass
class HarnessResult:
    """Consolidated result from all audit steps."""

    generated_at: str = ""
    repo_root: str = ""
    steps: list[StepResult] = field(default_factory=list)
    overall_passed: bool = True
    overall_exit_code: int = 0
    summary: dict[str, Any] = field(default_factory=dict)


def run_step(
    name: str,
    cmd: list[str],
    root: Path,
    fail_on_nonzero: bool = True,
) -> StepResult:
    """Run a subprocess and capture results."""
    result = StepResult(name=name, exit_code=0)

    # Check the script exists
    if len(cmd) >= 2 and cmd[0] in (sys.executable, "python3", "python"):
        script_path = root / cmd[1]
        if not script_path.exists():
            result.exit_code = 2
            result.stderr = f"Script not found: {cmd[1]}"
            result.passed = False
            return result

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT_S,
        )
        result.exit_code = proc.returncode
        result.stdout = proc.stdout
        result.stderr = proc.stderr
        if fail_on_nonzero and proc.returncode != 0:
            result.passed = False
    except subprocess.TimeoutExpired:
        result.exit_code = TIMEOUT_EXIT_CODE
        result.stderr = f"TIMEOUT: {name} exceeded {STEP_TIMEOUT_S}s"
        result.passed = False
    except FileNotFoundError as e:
        result.exit_code = 2
        result.stderr = f"Command not found: {e}"
        result.passed = False

    return result


def run_steps_concurrently(
    step_specs: list[tuple[str, list[str], bool]],
    root: Path,
) -> dict[str, StepResult]:
    """Run independent audit steps in a bounded thread pool, keyed by name.

    Each ``(name, cmd, fail_on_nonzero)`` triple is executed via ``run_step``.
    The steps write disjoint artifacts and share no data, so overlapping their
    wall-clock is safe — ``subprocess.run`` releases the GIL while it waits on
    the child process. Results are returned by name so the caller can reassemble
    them in a deterministic order; the concurrency is capped at the number of
    steps, so there is no unbounded process fan-out.
    """
    results: dict[str, StepResult] = {}
    if not step_specs:
        return results
    with ThreadPoolExecutor(max_workers=len(step_specs)) as pool:
        futures = [pool.submit(run_step, name, cmd, root, fail_on_nonzero) for name, cmd, fail_on_nonzero in step_specs]
        for fut in as_completed(futures):
            res = fut.result()
            results[res.name] = res
    return results


def collect_artifacts(root: Path) -> dict[str, str]:
    """Read all generated artifacts from artifacts/ directory."""
    artifacts_dir = root / "artifacts"
    collected: dict[str, str] = {}
    if not artifacts_dir.exists():
        return collected
    for f in sorted(artifacts_dir.iterdir()):
        if f.is_file() and f.suffix in (".md", ".json"):
            try:
                collected[f.name] = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                collected[f.name] = "<read error>"
    return collected


def parse_coverage_matrix(root: Path) -> dict[str, Any]:
    """Parse the coverage matrix JSON if it exists."""
    matrix_path = root / "artifacts" / "coverage_matrix.json"
    if not matrix_path.exists():
        return {}
    try:
        return json.loads(matrix_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def parse_audit_report(root: Path) -> dict[str, int]:
    """Parse the audit report to count findings by severity."""
    report_path = root / "artifacts" / "audit_report.md"
    if not report_path.exists():
        return {}
    try:
        content = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    current_severity = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## CRITICAL"):
            current_severity = "CRITICAL"
        elif stripped.startswith("## HIGH"):
            current_severity = "HIGH"
        elif stripped.startswith("## MEDIUM"):
            current_severity = "MEDIUM"
        elif stripped.startswith("## LOW"):
            current_severity = "LOW"
        elif stripped.startswith("### ") and current_severity:
            counts[current_severity] += 1
    return counts


# ── Baseline ratchet ────────────────────────────────────────────────────────
#
# tools/audit_baseline.json records the finding counts per severity and the
# spec-coverage totals of the last deliberate re-baseline. Every run compares
# against it and fails (exit 1) when the audit got worse:
#   - any severity count rises (CRITICAL, HIGH, MEDIUM and LOW alike: a pure
#     ratchet — every MEDIUM rule is a precise contract check, so a new finding
#     is a regression, not noise, and a tolerance band would let them pile up);
#   - spec IMPLEMENTED drops, or spec MISSING rises.
# PARTIAL and total are reported, never judged: they move whenever a feature
# changes state, in either direction. Improvements pass and are reported with
# the command that lowers the baseline. A missing, unreadable or malformed
# baseline, or a run whose counts are incomplete or stale, fails closed (exit 2).

BASELINE_STEP = "Baseline Ratchet"
DEFAULT_BASELINE = Path("tools") / "audit_baseline.json"
BASELINE_SCHEMA = "l9.audit_baseline.v1"
UPDATE_BASELINE_CMD = "make harness-baseline"
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
COVERAGE_KEYS = ("IMPLEMENTED", "PARTIAL", "MISSING", "total")
# metric -> the direction a change must not take; None = reported only.
RATCHET_RULES: dict[str, str | None] = {
    **{f"findings.{sev}": "rise" for sev in SEVERITIES},
    "spec_coverage.IMPLEMENTED": "drop",
    "spec_coverage.PARTIAL": None,
    "spec_coverage.MISSING": "rise",
    "spec_coverage.total": None,
}
BASELINE_META = {
    "l9_schema": 1,
    "origin": "engine-specific",
    "engine": "graph",
    "layer": ["audit"],
    "tags": ["audit", "baseline", "ratchet"],
    "owner": "platform",
    "status": "active",
}


class BaselineError(Exception):
    """The ratchet cannot reach a verdict: baseline or current counts missing, malformed or stale."""


@dataclass
class MetricDelta:
    metric: str
    baseline: int
    current: int
    verdict: str  # "regression" | "improvement" | "unchanged" | "changed"

    @property
    def delta(self) -> int:
        return self.current - self.baseline


@dataclass
class RatchetResult:
    deltas: list[MetricDelta]

    @property
    def regressions(self) -> list[MetricDelta]:
        return [d for d in self.deltas if d.verdict == "regression"]

    @property
    def improvements(self) -> list[MetricDelta]:
        return [d for d in self.deltas if d.verdict == "improvement"]

    @property
    def passed(self) -> bool:
        return not self.regressions


def _counts(section: Any, keys: tuple[str, ...], where: str) -> dict[str, int]:
    if not isinstance(section, dict):
        raise BaselineError(f"{where}: expected an object with {list(keys)}, got {type(section).__name__}")
    missing = [k for k in keys if k not in section]
    if missing:
        raise BaselineError(f"{where}: missing {missing}")
    bad = [k for k in keys if type(section[k]) is not int or section[k] < 0]
    if bad:
        raise BaselineError(f"{where}: {bad} must be non-negative integers")
    return {k: section[k] for k in keys}


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    """Read and validate the baseline. Raises BaselineError; never returns a default."""
    if not path.is_file():
        raise BaselineError(
            f"baseline {path} not found. The ratchet fails closed without one; "
            f"create it deliberately with `{UPDATE_BASELINE_CMD}` and commit it."
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"baseline {path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != BASELINE_SCHEMA:
        raise BaselineError(f"baseline {path}: schema must be {BASELINE_SCHEMA!r}")
    return {
        "findings": _counts(doc.get("findings"), SEVERITIES, f"baseline {path} findings"),
        "spec_coverage": _counts(doc.get("spec_coverage"), COVERAGE_KEYS, f"baseline {path} spec_coverage"),
    }


def current_counts(audit_counts: dict[str, int], coverage_totals: dict[str, Any]) -> dict[str, dict[str, int]]:
    """This run's counts in baseline shape. Raises BaselineError when either source is incomplete."""
    return {
        "findings": _counts(audit_counts, SEVERITIES, "current architecture audit counts"),
        "spec_coverage": _counts(coverage_totals, COVERAGE_KEYS, "current spec coverage totals"),
    }


def compare_to_baseline(baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]) -> RatchetResult:
    deltas: list[MetricDelta] = []
    for metric, forbidden in RATCHET_RULES.items():
        section, key = metric.split(".", 1)
        before, now = baseline[section][key], current[section][key]
        if now == before:
            verdict = "unchanged"
        elif forbidden is None:
            verdict = "changed"
        elif (now > before) == (forbidden == "rise"):
            verdict = "regression"
        else:
            verdict = "improvement"
        deltas.append(MetricDelta(metric, before, now, verdict))
    return RatchetResult(deltas)


def fresh_artifact(path: Path, since: float) -> bool:
    """*path* was written by this run. artifacts/ is tracked, so an old copy must never stand in."""
    try:
        return path.stat().st_mtime >= since
    except OSError:
        return False


def baseline_document(current: dict[str, dict[str, int]], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "_l9_meta": meta or BASELINE_META,
        "schema": BASELINE_SCHEMA,
        "description": (
            "Audit-harness ratchet (tools/audit_harness.py). The harness fails when a finding count "
            "rises, spec IMPLEMENTED drops or spec MISSING rises against these numbers. "
            f"Re-baseline deliberately with `{UPDATE_BASELINE_CMD}` and commit the result."
        ),
        "findings": current["findings"],
        "spec_coverage": current["spec_coverage"],
    }


def write_baseline(path: Path, current: dict[str, dict[str, int]]) -> None:
    """Write the baseline, keeping an existing ``_l9_meta`` header, in the layout Biome keeps stable."""
    try:
        from tools.spec_extract import _biome_json
    except ImportError:  # run as `python tools/audit_harness.py`: tools/ is sys.path[0]
        from spec_extract import _biome_json  # type: ignore[import-not-found,no-redef]

    meta = None
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and isinstance(existing.get("_l9_meta"), dict):
            meta = existing["_l9_meta"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_biome_json(baseline_document(current, meta)), encoding="utf-8")


def ratchet_lines(ratchet: RatchetResult | None, error: str | None, baseline_rel: str) -> list[str]:
    """The baseline-vs-current section of harness_report.md (and so of the PR comment)."""
    lines = ["## Baseline Ratchet", "", f"Baseline: `{baseline_rel}`", ""]
    if ratchet is None:
        lines += [f"❌ **Not evaluated — fails closed:** {error}", ""]
        return lines
    icons = {"regression": "❌ regression", "improvement": "✅ improved", "unchanged": "—", "changed": "reported only"}
    lines += ["| Metric | Baseline | Current | Δ | Result |", "|--------|----------|---------|---|--------|"]
    for d in ratchet.deltas:
        lines.append(f"| {d.metric} | {d.baseline} | {d.current} | {d.delta:+d} | {icons[d.verdict]} |")
    lines.append("")
    if ratchet.regressions:
        lines.append(
            f"❌ **{len(ratchet.regressions)} regression(s) against the baseline.** Fix them; if the change is "
            f"intended, re-baseline with `{UPDATE_BASELINE_CMD}` and commit `{baseline_rel}` with the reason."
        )
    else:
        lines.append("✅ No regressions against the baseline.")
    if ratchet.improvements:
        lines.append("")
        lines.append(
            f"🎉 {len(ratchet.improvements)} metric(s) improved. Lock the gain in: run `{UPDATE_BASELINE_CMD}` "
            f"and commit `{baseline_rel}`."
        )
    lines.append("")
    return lines


def write_harness_report(
    root: Path,
    harness: HarnessResult,
    audit_counts: dict[str, int],
    coverage: dict[str, Any],
    ratchet_section: list[str] | None = None,
) -> Path:
    """Write the consolidated harness report."""
    out_dir = root / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "harness_report.md"

    lines: list[str] = []
    lines.append("# L9 Audit Harness Report")
    lines.append("")
    lines.append(f"- **Generated:** {harness.generated_at}")
    lines.append(f"- **Repo root:** `{harness.repo_root}`")
    lines.append(f"- **Overall result:** {'✅ PASSED' if harness.overall_passed else '❌ FAILED'}")
    lines.append(f"- **Exit code:** {harness.overall_exit_code}")
    lines.append("")

    # Step summary table
    lines.append("## Step Results")
    lines.append("")
    lines.append("| Step | Status | Exit Code | Notes |")
    lines.append("|------|--------|-----------|-------|")
    for step in harness.steps:
        if step.skipped:
            status = "⏭️ Skipped"
        elif step.passed:
            status = "✅ Passed"
        else:
            status = "❌ Failed"
        notes = ""
        if step.stderr and not step.passed:
            first_line = step.stderr.strip().splitlines()[0][:80] if step.stderr.strip() else ""
            notes = first_line
        lines.append(f"| {step.name} | {status} | {step.exit_code} | {notes} |")
    lines.append("")

    # Architecture audit findings
    if audit_counts:
        lines.append("## Architecture Audit Findings")
        lines.append("")
        lines.append("| Severity | Count |")
        lines.append("|----------|-------|")
        for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
            count = audit_counts.get(sev, 0)
            icon_medium = "🟡" if sev == "MEDIUM" else "🔵"
            icon_high = "🟠" if sev == "HIGH" else icon_medium
            icon = "🔴" if sev == "CRITICAL" else icon_high
            lines.append(f"| {icon} {sev} | {count} |")
        lines.append("")
        lines.append("See `artifacts/audit_report.md` for full details.")
        lines.append("")

    # Spec coverage summary
    if coverage and "totals" in coverage:
        totals = coverage["totals"]
        lines.append("## Spec Coverage")
        lines.append("")
        lines.append(f"- ✅ Implemented: {totals.get('IMPLEMENTED', 0)}")
        lines.append(f"- ⚠️ Partial: {totals.get('PARTIAL', 0)}")
        lines.append(f"- ❌ Missing: {totals.get('MISSING', 0)}")
        lines.append(f"- **Total features:** {totals.get('total', 0)}")
        lines.append("")

        if "categories" in coverage:
            lines.append("| Category | Implemented | Partial | Missing | Total |")
            lines.append("|----------|-------------|---------|---------|-------|")
            for cat, data in coverage["categories"].items():
                lines.append(
                    f"| {cat} | {data['IMPLEMENTED']} | {data['PARTIAL']} | {data['MISSING']} | {data['total']} |"
                )
            lines.append("")

        lines.append("See `artifacts/coverage_report.md` for full details.")
        lines.append("")

    if ratchet_section:
        lines.extend(ratchet_section)

    # What to do next
    lines.append("## Next Steps")
    lines.append("")
    if not harness.overall_passed:
        todo: list[str] = []
        if audit_counts.get("CRITICAL", 0) > 0:
            todo.append("**Fix CRITICAL findings first** — these are production blockers")
        if audit_counts.get("HIGH", 0) > 0:
            todo.append("**Fix HIGH findings** — these block merge")
        if any(s.name == BASELINE_STEP and not s.passed for s in harness.steps):
            todo.append("**Resolve the baseline ratchet failure** — see Baseline Ratchet above")
        todo.append("Re-run `make harness` after fixes to verify")
        lines.extend(f"{i}. {item}" for i, item in enumerate(todo, start=1))
    else:
        lines.append("All checks passed. Safe to merge.")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def is_infra_error(step: StepResult) -> bool:
    """The step failed without reaching a verdict (missing script, command not found, timeout)."""
    return not step.passed and step.exit_code in INFRA_EXIT_CODES


def overall_exit_code(steps: list[StepResult]) -> int:
    """Harness exit code per the module docstring.

    2 when any blocking step failed with an infrastructure error (exit 2, or
    the harness's own timeout, 124), else 1 when any blocking step failed,
    else 0. Skipped steps and informational steps (already marked passed,
    e.g. non-strict Spec Coverage) never block.
    """
    failed = [s for s in steps if not s.passed and not s.skipped]
    if any(is_infra_error(s) for s in failed):
        return INFRA_EXIT_CODE
    return 1 if failed else 0


def run_counts(
    root: Path,
    source_steps: list[StepResult],
    audit_counts: dict[str, int],
    coverage: dict[str, Any],
    *,
    started: float,
) -> dict[str, dict[str, int]]:
    """This run's counts, only when the steps that produce them ran to a verdict and wrote them now."""
    broken = [s.name for s in source_steps if s.exit_code in INFRA_EXIT_CODES]
    if broken:
        raise BaselineError(f"counts are incomplete: {broken} did not run to a verdict")
    stale = [
        rel
        for rel in ("artifacts/audit_report.md", "artifacts/coverage_matrix.json")
        if not fresh_artifact(root / rel, started)
    ]
    if stale:
        raise BaselineError(f"{stale} were not written by this run; refusing to judge stale counts")
    return current_counts(audit_counts, coverage.get("totals", {}))


def ratchet_summary(baseline_rel: str, ratchet: RatchetResult | None, error: str | None) -> dict[str, Any]:
    """The ratchet block of the --json summary."""
    if ratchet is None:
        return {"path": baseline_rel, "status": "error", "error": error}
    return {
        "path": baseline_rel,
        "status": "passed" if ratchet.passed else "regressed",
        "metrics": {
            d.metric: {"baseline": d.baseline, "current": d.current, "verdict": d.verdict} for d in ratchet.deltas
        },
        "regressions": [d.metric for d in ratchet.regressions],
        "improvements": [d.metric for d in ratchet.improvements],
    }


def evaluate_ratchet(
    root: Path,
    baseline_path: Path,
    source_steps: list[StepResult],
    audit_counts: dict[str, int],
    coverage: dict[str, Any],
    *,
    started: float,
    update: bool = False,
) -> tuple[StepResult, RatchetResult | None, str | None]:
    """Compare this run with the baseline (after rewriting it when *update*).

    Returns the ratchet step (exit 1 on a regression, 2 when no verdict is
    possible), the comparison, and the reason there is none.
    """
    step = StepResult(name=BASELINE_STEP, exit_code=0)
    try:
        current = run_counts(root, source_steps, audit_counts, coverage, started=started)
        if update:
            write_baseline(baseline_path, current)
        ratchet = compare_to_baseline(load_baseline(baseline_path), current)
    except BaselineError as exc:
        step.exit_code, step.passed, step.stderr = INFRA_EXIT_CODE, False, str(exc)
        return step, None, str(exc)
    if not ratchet.passed:
        step.exit_code, step.passed = 1, False
        step.stderr = "regressed: " + ", ".join(f"{d.metric} {d.baseline}->{d.current}" for d in ratchet.regressions)
    return step, ratchet, None


def main() -> int:
    parser = argparse.ArgumentParser(description="L9 Audit Harness — orchestrates all static analysis")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on MISSING spec features (default: only CRITICAL/HIGH arch findings)",
    )
    parser.add_argument(
        "--skip-contracts",
        action="store_true",
        help="Skip contract wiring verification",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON summary to stdout",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repo root directory (default: cwd)",
    )
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help=f"Ratchet baseline, relative to --root (default: {DEFAULT_BASELINE})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite the baseline from this run's counts (a deliberate re-baseline; commit the result)",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    baseline_path = root / args.baseline
    python = sys.executable
    # Artifacts older than this were not written by this run (1 s covers
    # coarse filesystem timestamps; a tracked copy from checkout is far older).
    started = time.time() - 1.0

    harness = HarnessResult(
        generated_at=datetime.now(UTC).isoformat(),
        repo_root=str(root),
    )

    print("\n╔══════════════════════════════════════╗")
    print("║  L9 Audit Harness                    ║")
    print("╚══════════════════════════════════════╝\n")

    # ── Run the independent audit steps concurrently ────────
    # Each step scans the tree and writes its own disjoint artifacts
    # (audit_engine.py → audit_report.md; spec_extract.py →
    # coverage_matrix.json / coverage_report.md / spec_checklist.json;
    # verify_contracts.py → stdout only), so they have no ordering or data
    # dependency. Running them in a bounded thread pool overlaps their
    # wall-clock — subprocess.run releases the GIL while waiting on the child —
    # without changing findings, the report, or the exit code. Results are
    # reassembled in the fixed 1/2/3 order below so output stays deterministic.
    spec_fail_on = "MISSING" if args.strict else "NONE"
    step_specs: list[tuple[str, list[str], bool]] = [
        ("Architecture Audit", [python, "tools/audit_engine.py"], True),
        ("Spec Coverage", [python, "tools/spec_extract.py", "--fail-on", spec_fail_on], args.strict),
    ]
    if not args.skip_contracts:
        step_specs.append((CONTRACT_WIRING_STEP, [python, "tools/verify_contracts.py"], True))

    print(f"Running {len(step_specs)} audit step(s) concurrently...\n")
    results = run_steps_concurrently(step_specs, root)

    # ── Step 1: Architecture Audit ──────────────────────────
    step1 = results["Architecture Audit"]
    harness.steps.append(step1)
    print("[1/4] Architecture audit...")
    if step1.passed:
        print("  ✅ No CRITICAL/HIGH findings")
    elif is_infra_error(step1):
        print(f"  ⚠️  Infrastructure error: {step1.stderr.strip()[:100]}")
    else:
        print("  ❌ CRITICAL or HIGH findings detected")
    if step1.stdout:
        for line in step1.stdout.strip().splitlines():
            print(f"     {line}")

    # ── Step 2: Spec Coverage ───────────────────────────────
    step2 = results["Spec Coverage"]
    # In non-strict mode, spec failures are informational only
    if not args.strict:
        step2.passed = True
    harness.steps.append(step2)
    print("\n[2/4] Spec coverage scan...")
    if step2.passed:
        print("  ✅ Spec coverage scan complete")
    else:
        print("  ❌ Missing spec features (--strict mode)")
    if step2.stdout:
        for line in step2.stdout.strip().splitlines()[-5:]:
            print(f"     {line}")

    # ── Step 3: Contract Wiring ─────────────────────────────
    if args.skip_contracts:
        step3 = StepResult(name=CONTRACT_WIRING_STEP, exit_code=0, skipped=True)
        print("\n[3/4] Contract wiring... ⏭️  skipped")
    else:
        step3 = results[CONTRACT_WIRING_STEP]
        print("\n[3/4] Contract wiring check...")
        if step3.passed:
            print("  ✅ All contracts present and wired")
        elif is_infra_error(step3):
            # Infrastructure error (script missing / command not found / timeout): the wiring
            # was not verified, so the step fails and the harness exits 2. Only an
            # explicit --skip-contracts skips this step.
            print(f"  ⚠️  Infrastructure error: {step3.stderr.strip()[:100]}")
        else:
            print("  ❌ Contract wiring issues detected")
            if step3.stderr:
                for line in step3.stderr.strip().splitlines()[:5]:
                    print(f"     {line}")
    harness.steps.append(step3)

    # ── Step 4: Baseline Ratchet ────────────────────────────
    audit_counts = parse_audit_report(root)
    coverage = parse_coverage_matrix(root)
    baseline_rel = Path(args.baseline).as_posix()
    print(f"\n[4/4] Baseline ratchet ({baseline_rel})...")
    step4, ratchet, ratchet_error = evaluate_ratchet(
        root, baseline_path, [step1, step2], audit_counts, coverage, started=started, update=args.update_baseline
    )
    harness.steps.append(step4)
    if ratchet is None:
        print(f"  ⚠️  Not evaluated, failing closed: {ratchet_error}")
    else:
        if args.update_baseline:
            print(f"  ✍️  Baseline rewritten from this run: {baseline_rel} (commit it deliberately)")
        for d in ratchet.deltas:
            if d.verdict != "unchanged":
                print(f"     {d.metric}: {d.baseline} -> {d.current} ({d.verdict})")
        if ratchet.passed:
            print("  ✅ No regressions against the baseline")
        else:
            print(f"  ❌ {len(ratchet.regressions)} regression(s); if intended, re-baseline: {UPDATE_BASELINE_CMD}")
        if ratchet.improvements:
            print(f"  🎉 Improved — lower the baseline: {UPDATE_BASELINE_CMD}, then commit {baseline_rel}")

    # ── Consolidate ─────────────────────────────────────────
    harness.overall_passed = all(s.passed for s in harness.steps)
    harness.overall_exit_code = overall_exit_code(harness.steps)

    harness.summary = {
        "audit_findings": audit_counts,
        "spec_coverage": coverage.get("totals", {}),
        "steps_passed": sum(1 for s in harness.steps if s.passed),
        "steps_failed": sum(1 for s in harness.steps if not s.passed and not s.skipped),
        "steps_skipped": sum(1 for s in harness.steps if s.skipped),
    }

    harness.summary["baseline"] = ratchet_summary(baseline_rel, ratchet, ratchet_error)

    report_path = write_harness_report(
        root, harness, audit_counts, coverage, ratchet_lines(ratchet, ratchet_error, baseline_rel)
    )

    # ── Final Output ────────────────────────────────────────
    print("\n" + "═" * 42)
    if harness.overall_passed:
        print("  ✅ HARNESS PASSED — safe to merge")
    else:
        print("  ❌ HARNESS FAILED — fix issues before merge")
    print("═" * 42)
    print("\nReports:")
    print(f"  Consolidated:  {report_path}")
    print("  Arch audit:    artifacts/audit_report.md")
    print("  Spec coverage: artifacts/coverage_report.md")
    print("  Coverage JSON: artifacts/coverage_matrix.json")

    if args.json:
        summary_json = {
            "generated_at": harness.generated_at,
            "overall_passed": harness.overall_passed,
            "exit_code": harness.overall_exit_code,
            "audit_findings": audit_counts,
            "spec_coverage": coverage.get("totals", {}),
            "baseline": harness.summary["baseline"],
            "steps": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "skipped": s.skipped,
                    "exit_code": s.exit_code,
                }
                for s in harness.steps
            ],
        }
        print("\n--- JSON Summary ---")
        print(json.dumps(summary_json, indent=2))

    return harness.overall_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
