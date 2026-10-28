"""Unit tests — audit harness concurrent step execution.

Covers the throughput change in ``tools/audit_harness.py``: the three
independent static-analysis steps now run in a bounded thread pool via
``run_steps_concurrently`` instead of strictly one after another, while results
stay keyed by name so the caller can reassemble them in deterministic order.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import audit_harness as ah  # noqa: E402


def _write_script(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


@pytest.mark.unit
def test_run_step_reports_missing_script(tmp_path: Path) -> None:
    res = ah.run_step("missing", [sys.executable, "nope.py"], tmp_path)
    assert res.exit_code == 2
    assert res.passed is False


@pytest.mark.unit
def test_run_step_captures_success(tmp_path: Path) -> None:
    # Prefer stdout.write so the fixture body does not trip terminology-guard.
    _write_script(tmp_path, "ok.py", "import sys; sys.stdout.write('hello\\n')")
    res = ah.run_step("ok", [sys.executable, "ok.py"], tmp_path)
    assert res.exit_code == 0
    assert res.passed is True
    assert "hello" in res.stdout


@pytest.mark.unit
def test_run_steps_concurrently_returns_all_by_name(tmp_path: Path) -> None:
    _write_script(tmp_path, "a.py", "import sys; sys.stdout.write('a\\n')")
    _write_script(tmp_path, "b.py", "import sys; sys.exit(1)")
    _write_script(tmp_path, "c.py", "import sys; sys.stdout.write('c\\n')")
    specs = [
        ("A", [sys.executable, "a.py"], True),
        ("B", [sys.executable, "b.py"], True),
        ("C", [sys.executable, "c.py"], True),
    ]

    results = ah.run_steps_concurrently(specs, tmp_path)

    assert set(results) == {"A", "B", "C"}
    assert results["A"].passed is True
    assert results["B"].passed is False  # non-zero exit propagates
    assert results["B"].exit_code == 1
    assert results["C"].stdout.strip() == "c"


@pytest.mark.unit
def test_run_steps_concurrently_overlaps_wall_clock(tmp_path: Path) -> None:
    # Three ~0.4s sleeps must finish well under the 1.2s serial sum when run
    # concurrently, proving the steps actually overlap.
    _write_script(tmp_path, "sleep.py", "import time; time.sleep(0.4)")
    specs = [(f"S{i}", [sys.executable, "sleep.py"], True) for i in range(3)]

    start = time.perf_counter()
    results = ah.run_steps_concurrently(specs, tmp_path)
    elapsed = time.perf_counter() - start

    assert set(results) == {"S0", "S1", "S2"}
    assert all(r.passed for r in results.values())
    # Serial wall clock would be ~1.2s; allow CI scheduler jitter above 1.0s
    # while still proving overlap (must beat serial sum).
    assert elapsed < 1.15


@pytest.mark.unit
def test_run_steps_concurrently_empty(tmp_path: Path) -> None:
    assert ah.run_steps_concurrently([], tmp_path) == {}


def _harness_root(tmp_path: Path, *, with_contract_wiring: bool) -> Path:
    """A minimal root holding real copies of the harness's step scripts."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("audit_engine.py", "audit_rules.yaml", "spec_extract.py", "l9_template_manifest.yaml"):
        (tools / name).write_text((REPO_ROOT / "tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    if with_contract_wiring:
        _write_script(tools, "verify_contracts.py", "import sys; sys.exit(0)")
    return tmp_path


def _run_harness(monkeypatch: pytest.MonkeyPatch, root: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["audit_harness.py", "--root", str(root), *extra])
    return ah.main()


@pytest.mark.unit
def test_missing_contract_wiring_script_fails_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RG-168 (RU-133): exit 2 from a required step is an infrastructure error, not a pass."""
    root = _harness_root(tmp_path, with_contract_wiring=False)

    rc = _run_harness(monkeypatch, root, "--json")

    out = capsys.readouterr().out
    assert rc == 2
    assert "safe to merge" not in out
    wiring = next(
        s
        for s in ah.json.loads(out.split("--- JSON Summary ---", 1)[1])["steps"]
        if s["name"] == ah.CONTRACT_WIRING_STEP
    )
    assert wiring == {"name": ah.CONTRACT_WIRING_STEP, "passed": False, "skipped": False, "exit_code": 2}
    report = (root / "artifacts" / "harness_report.md").read_text(encoding="utf-8")
    assert "Safe to merge" not in report
    assert "**Exit code:** 2" in report


@pytest.mark.unit
def test_skip_contracts_keeps_explicit_skip_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _harness_root(tmp_path, with_contract_wiring=False)

    rc = _run_harness(monkeypatch, root, "--skip-contracts")

    assert rc == 0
    assert "safe to merge" in capsys.readouterr().out


@pytest.mark.unit
def test_present_contract_wiring_script_passes_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _harness_root(tmp_path, with_contract_wiring=True)

    rc = _run_harness(monkeypatch, root)

    assert rc == 0
    assert "safe to merge" in capsys.readouterr().out


@pytest.mark.unit
def test_overall_exit_code_precedence() -> None:
    """2 when any required step hit an infrastructure error, else 1 on failure, else 0."""
    ok = ah.StepResult(name="a", exit_code=0)
    failed = ah.StepResult(name="b", exit_code=1, passed=False)
    infra = ah.StepResult(name="c", exit_code=2, passed=False)
    skipped = ah.StepResult(name="d", exit_code=0, skipped=True)
    assert ah.overall_exit_code([ok, skipped]) == 0
    assert ah.overall_exit_code([ok, failed]) == 1
    assert ah.overall_exit_code([failed, infra]) == 2
    # An informational step forced to passed (non-strict Spec Coverage) never blocks.
    informational = ah.StepResult(name="e", exit_code=2, passed=True)
    assert ah.overall_exit_code([ok, informational]) == 0


@pytest.mark.unit
def test_run_step_timeout_is_an_infrastructure_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ah, "STEP_TIMEOUT_S", 0.5)
    _write_script(tmp_path, "slow.py", "import time; time.sleep(30)")

    res = ah.run_step("slow", [sys.executable, "slow.py"], tmp_path)

    assert res.exit_code == ah.TIMEOUT_EXIT_CODE
    assert res.passed is False
    assert "TIMEOUT: slow exceeded 0.5s" in res.stderr
    assert ah.is_infra_error(res)


@pytest.mark.unit
def test_overall_exit_code_classifies_timeout_as_infrastructure() -> None:
    """A step the harness timed out (124) never reached a verdict: exit 2, not 1."""
    failed = ah.StepResult(name="b", exit_code=1, passed=False)
    timed_out = ah.StepResult(name="t", exit_code=ah.TIMEOUT_EXIT_CODE, passed=False)
    assert ah.overall_exit_code([timed_out]) == 2
    assert ah.overall_exit_code([failed, timed_out]) == 2
    assert not ah.is_infra_error(failed)


@pytest.mark.unit
def test_timed_out_contract_wiring_exits_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RU-133 review: a step the harness times out is an infrastructure error (exit 2), not a finding."""
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_script(tools, "audit_engine.py", "import sys; sys.exit(0)")
    _write_script(tools, "spec_extract.py", "import sys; sys.exit(0)")
    _write_script(tools, "verify_contracts.py", "import time; time.sleep(60)")
    monkeypatch.setattr(ah, "STEP_TIMEOUT_S", 1)

    rc = _run_harness(monkeypatch, tmp_path, "--json")

    out = capsys.readouterr().out
    assert rc == 2
    assert "Infrastructure error: TIMEOUT: Contract Wiring exceeded 1s" in out
    assert "safe to merge" not in out
    wiring = next(
        s
        for s in ah.json.loads(out.split("--- JSON Summary ---", 1)[1])["steps"]
        if s["name"] == ah.CONTRACT_WIRING_STEP
    )
    assert wiring["exit_code"] == ah.TIMEOUT_EXIT_CODE
    assert wiring["passed"] is False
