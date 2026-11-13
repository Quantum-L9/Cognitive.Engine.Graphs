"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [audit, baseline, ratchet, regression]
owner: platform
status: active
--- /L9_META ---

Unit tests — the audit harness baseline ratchet (``tools/audit_harness.py``).

The harness compares each run with ``tools/audit_baseline.json`` and fails
when a finding count rises, spec IMPLEMENTED drops or spec MISSING rises.
Equal and better runs pass, and improvements are reported with the command
that lowers the baseline. A missing or malformed baseline, or counts this run
did not produce, fail closed with exit 2 instead of passing silently.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from tools import audit_harness as ah

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
EQUAL = {
    "findings": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 2, "LOW": 1},
    "spec_coverage": {"IMPLEMENTED": 21, "PARTIAL": 15, "MISSING": 5, "total": 41},
}


def _with(section: str, key: str, value: int) -> dict[str, dict[str, int]]:
    counts = {s: dict(v) for s, v in EQUAL.items()}
    counts[section][key] = value
    return counts


def _write_baseline(path: Path, counts: dict[str, dict[str, int]] = EQUAL) -> Path:
    path.write_text(json.dumps(ah.baseline_document(counts)), encoding="utf-8")
    return path


# ── comparison ──────────────────────────────────────────────────────────────


def test_equal_counts_pass() -> None:
    result = ah.compare_to_baseline(EQUAL, EQUAL)

    assert result.passed
    assert {d.verdict for d in result.deltas} == {"unchanged"}
    assert [d.metric for d in result.deltas] == list(ah.RATCHET_RULES)


@pytest.mark.parametrize("severity", ah.SEVERITIES)
def test_any_finding_increase_fails(severity: str) -> None:
    current = _with("findings", severity, EQUAL["findings"][severity] + 1)

    result = ah.compare_to_baseline(EQUAL, current)

    assert not result.passed
    assert [(d.metric, d.baseline, d.current) for d in result.regressions] == [
        (f"findings.{severity}", EQUAL["findings"][severity], EQUAL["findings"][severity] + 1)
    ]


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("findings", "MEDIUM", 0),
        ("findings", "LOW", 0),
        ("spec_coverage", "IMPLEMENTED", 22),
        ("spec_coverage", "MISSING", 4),
    ],
)
def test_improvement_passes_and_is_reported(section: str, key: str, value: int) -> None:
    result = ah.compare_to_baseline(EQUAL, _with(section, key, value))

    assert result.passed
    assert [d.metric for d in result.improvements] == [f"{section}.{key}"]
    report = "\n".join(ah.ratchet_lines(result, None, "tools/audit_baseline.json"))
    assert "improved" in report
    assert ah.UPDATE_BASELINE_CMD in report


@pytest.mark.parametrize(("key", "value"), [("IMPLEMENTED", 20), ("MISSING", 6)])
def test_spec_coverage_regression_fails(key: str, value: int) -> None:
    result = ah.compare_to_baseline(EQUAL, _with("spec_coverage", key, value))

    assert [d.metric for d in result.regressions] == [f"spec_coverage.{key}"]


@pytest.mark.parametrize(("key", "value"), [("PARTIAL", 16), ("PARTIAL", 14), ("total", 42), ("total", 40)])
def test_partial_and_total_are_reported_not_judged(key: str, value: int) -> None:
    result = ah.compare_to_baseline(EQUAL, _with("spec_coverage", key, value))

    assert result.passed
    assert result.improvements == []
    assert [(d.metric, d.verdict) for d in result.deltas if d.verdict != "unchanged"] == [
        (f"spec_coverage.{key}", "changed")
    ]


def test_regression_report_shows_baseline_vs_current_and_how_to_rebaseline() -> None:
    result = ah.compare_to_baseline(EQUAL, _with("findings", "HIGH", 3))

    report = "\n".join(ah.ratchet_lines(result, None, "tools/audit_baseline.json"))

    assert "| findings.HIGH | 0 | 3 | +3 | ❌ regression |" in report
    assert "1 regression(s)" in report
    assert ah.UPDATE_BASELINE_CMD in report


# ── loading: fail closed ────────────────────────────────────────────────────


def test_missing_baseline_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ah.BaselineError, match="not found.*fails closed"):
        ah.load_baseline(tmp_path / "audit_baseline.json")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "unreadable"),
        ("[]", "schema must be"),
        (json.dumps({"schema": "other", **EQUAL}), "schema must be"),
        (json.dumps({"schema": ah.BASELINE_SCHEMA, "spec_coverage": EQUAL["spec_coverage"]}), "findings"),
        (json.dumps({**ah.baseline_document(EQUAL), "findings": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0}}), "LOW"),
        (json.dumps({**ah.baseline_document(_with("findings", "HIGH", -1))}), "non-negative integers"),
        (
            json.dumps({**ah.baseline_document(EQUAL), "spec_coverage": {**EQUAL["spec_coverage"], "total": "41"}}),
            "total",
        ),
        (json.dumps({**ah.baseline_document(EQUAL), "findings": {**EQUAL["findings"], "LOW": True}}), "LOW"),
    ],
)
def test_malformed_baseline_is_an_error(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "audit_baseline.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ah.BaselineError, match=message):
        ah.load_baseline(path)


def test_committed_baseline_is_valid() -> None:
    baseline = ah.load_baseline(REPO_ROOT / ah.DEFAULT_BASELINE)

    assert set(baseline) == {"findings", "spec_coverage"}


# ── evaluate_ratchet: the harness step ──────────────────────────────────────


def _fresh_artifacts(root: Path) -> None:
    (root / "artifacts").mkdir(exist_ok=True)
    (root / "artifacts" / "audit_report.md").write_text("# report\n", encoding="utf-8")
    (root / "artifacts" / "coverage_matrix.json").write_text("{}", encoding="utf-8")


def _evaluate(root: Path, baseline: Path, current: dict[str, dict[str, int]], **kwargs: object):
    steps = kwargs.pop("steps", [ah.StepResult("Architecture Audit", 0), ah.StepResult("Spec Coverage", 0)])
    started = kwargs.pop("started", time.time() - 5)
    return ah.evaluate_ratchet(
        root,
        baseline,
        steps,  # type: ignore[arg-type]
        current["findings"],
        {"totals": current["spec_coverage"]},
        started=started,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_step_passes_on_equal_counts(tmp_path: Path) -> None:
    _fresh_artifacts(tmp_path)
    step, result, error = _evaluate(tmp_path, _write_baseline(tmp_path / "b.json"), EQUAL)

    assert (step.passed, step.exit_code, error) == (True, 0, None)
    assert result is not None
    assert result.passed


def test_step_fails_with_exit_1_on_regression(tmp_path: Path) -> None:
    _fresh_artifacts(tmp_path)
    step, _, _ = _evaluate(tmp_path, _write_baseline(tmp_path / "b.json"), _with("findings", "MEDIUM", 3))

    assert (step.passed, step.exit_code) == (False, 1)
    assert "findings.MEDIUM 2->3" in step.stderr
    assert ah.overall_exit_code([step]) == 1


def test_missing_baseline_fails_the_step_closed(tmp_path: Path) -> None:
    _fresh_artifacts(tmp_path)
    step, result, error = _evaluate(tmp_path, tmp_path / "absent.json", EQUAL)

    assert result is None
    assert (step.passed, step.exit_code) == (False, ah.INFRA_EXIT_CODE)
    assert error is not None
    assert "not found" in error
    assert ah.overall_exit_code([step]) == ah.INFRA_EXIT_CODE
    assert "Not evaluated — fails closed" in "\n".join(ah.ratchet_lines(None, error, "b.json"))


def test_incomplete_current_counts_fail_closed(tmp_path: Path) -> None:
    _fresh_artifacts(tmp_path)
    step, _, error = ah.evaluate_ratchet(
        tmp_path, _write_baseline(tmp_path / "b.json"), [], {}, {}, started=time.time() - 5
    )

    assert step.exit_code == ah.INFRA_EXIT_CODE
    assert error is not None
    assert "current architecture audit counts" in error


def test_counts_from_a_step_that_never_reached_a_verdict_fail_closed(tmp_path: Path) -> None:
    _fresh_artifacts(tmp_path)
    broken = [ah.StepResult("Architecture Audit", ah.TIMEOUT_EXIT_CODE, passed=False)]

    step, _, error = _evaluate(tmp_path, _write_baseline(tmp_path / "b.json"), EQUAL, steps=broken)

    assert step.exit_code == ah.INFRA_EXIT_CODE
    assert error is not None
    assert "Architecture Audit" in error


def test_stale_artifacts_fail_closed(tmp_path: Path) -> None:
    """artifacts/ is tracked: a checked-out copy must never be judged as this run's counts."""
    _fresh_artifacts(tmp_path)

    step, _, error = _evaluate(tmp_path, _write_baseline(tmp_path / "b.json"), EQUAL, started=time.time() + 60)

    assert step.exit_code == ah.INFRA_EXIT_CODE
    assert error is not None
    assert "not written by this run" in error


def test_update_writes_the_baseline_and_keeps_its_header(tmp_path: Path) -> None:
    _fresh_artifacts(tmp_path)
    path = tmp_path / "b.json"
    custom_meta = {**ah.BASELINE_META, "owner": "engine-team"}
    path.write_text(json.dumps({"_l9_meta": custom_meta, "schema": ah.BASELINE_SCHEMA}), encoding="utf-8")
    improved = _with("findings", "MEDIUM", 0)

    step, result, _ = _evaluate(tmp_path, path, improved, update=True)

    assert step.passed
    assert result is not None
    assert {d.verdict for d in result.deltas} == {"unchanged"}
    assert ah.load_baseline(path) == improved
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["_l9_meta"] == custom_meta
    first = path.read_text(encoding="utf-8")
    _evaluate(tmp_path, path, improved, update=True)
    assert path.read_text(encoding="utf-8") == first, "re-baselining unchanged counts must not churn the file"


# ── end to end: the real harness entrypoint over stub steps ─────────────────

STUB_ENGINE = """\
import pathlib
pathlib.Path("artifacts").mkdir(exist_ok=True)
body = ["# L9 Engine Audit Report", "## CRITICAL", "## HIGH", "## MEDIUM"]
body += ["### TRACE_MATCH_FLOW_ANCHORS"] * {medium}
body += ["## LOW"]
pathlib.Path("artifacts/audit_report.md").write_text("\\n".join(body) + "\\n")
"""
STUB_SPEC = """\
import json, pathlib
pathlib.Path("artifacts").mkdir(exist_ok=True)
totals = {{"IMPLEMENTED": {implemented}, "PARTIAL": 15, "MISSING": 5, "total": 41}}
pathlib.Path("artifacts/coverage_matrix.json").write_text(json.dumps({{"categories": {{}}, "totals": totals}}))
"""


def _stub_repo(root: Path, *, medium: int, implemented: int) -> None:
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "audit_engine.py").write_text(STUB_ENGINE.format(medium=medium), encoding="utf-8")
    (root / "tools" / "spec_extract.py").write_text(STUB_SPEC.format(implemented=implemented), encoding="utf-8")
    (root / "tools" / "verify_contracts.py").write_text("", encoding="utf-8")


def _run_harness(monkeypatch: pytest.MonkeyPatch, root: Path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["audit_harness.py", "--root", str(root), "--json", *args])
    return ah.main()


def _summary(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.split("--- JSON Summary ---", 1)[1])


# (MEDIUM now, IMPLEMENTED now, exit code, ratchet status) against MEDIUM 2 / IMPLEMENTED 21
RUN_CASES = [
    pytest.param((2, 21, 0, "passed"), id="equal"),
    pytest.param((1, 22, 0, "passed"), id="better"),
    pytest.param((3, 21, 1, "regressed"), id="medium-rose"),
    pytest.param((2, 20, 1, "regressed"), id="implemented-dropped"),
]


@pytest.mark.parametrize("case", RUN_CASES)
def test_harness_exit_code_follows_the_ratchet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: tuple[int, int, int, str]
) -> None:
    medium, implemented, exit_code, status = case
    _stub_repo(tmp_path, medium=medium, implemented=implemented)
    _write_baseline(tmp_path / ah.DEFAULT_BASELINE)

    rc = _run_harness(monkeypatch, tmp_path)

    assert rc == exit_code
    assert _summary(capsys)["baseline"]["status"] == status
    report = (tmp_path / "artifacts" / "harness_report.md").read_text(encoding="utf-8")
    assert "## Baseline Ratchet" in report
    assert f"| findings.MEDIUM | 2 | {medium} |" in report


def test_harness_without_a_baseline_exits_2_and_update_creates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_repo(tmp_path, medium=2, implemented=21)

    assert _run_harness(monkeypatch, tmp_path) == ah.INFRA_EXIT_CODE
    assert _summary(capsys)["baseline"]["status"] == "error"

    assert _run_harness(monkeypatch, tmp_path, "--update-baseline") == 0
    assert ah.load_baseline(tmp_path / ah.DEFAULT_BASELINE) == _with("findings", "LOW", 0)  # the stub emits no LOW
    capsys.readouterr()
    assert _run_harness(monkeypatch, tmp_path) == 0
    assert _summary(capsys)["baseline"]["status"] == "passed"
