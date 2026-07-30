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
    _write_script(tmp_path, "ok.py", "print('hello')")
    res = ah.run_step("ok", [sys.executable, "ok.py"], tmp_path)
    assert res.exit_code == 0
    assert res.passed is True
    assert "hello" in res.stdout


@pytest.mark.unit
def test_run_steps_concurrently_returns_all_by_name(tmp_path: Path) -> None:
    _write_script(tmp_path, "a.py", "print('a')")
    _write_script(tmp_path, "b.py", "import sys; sys.exit(1)")
    _write_script(tmp_path, "c.py", "print('c')")
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
    assert elapsed < 1.0


@pytest.mark.unit
def test_run_steps_concurrently_empty(tmp_path: Path) -> None:
    assert ah.run_steps_concurrently([], tmp_path) == {}
