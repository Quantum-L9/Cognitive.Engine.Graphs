"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [audit, auditors]
owner: platform
status: active
--- /L9_META ---

Unit tests for static-auditor classification and audit_dispatch exit codes
(RU-139 / RG-175).

* test_quality counts ``async def`` tests and ``with pytest.raises/warns``.
* query_performance reports a call once per enclosing loop body, never a
  call in a loop's own iterable, never twice for nested loops.
* ``audit_dispatch --strict`` exits non-zero when nothing could run or an
  auditor module failed to import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools import audit_dispatch as dispatch
from tools.auditors import query_performance as qp
from tools.auditors import test_quality as tq

pytestmark = pytest.mark.unit

ASYNC_ONLY = """import pytest

@pytest.mark.asyncio
async def test_does_nothing():
    x = 1  # zero assertions, async

@pytest.mark.asyncio
async def test_real():
    assert 1 == 1
"""

RAISES = """import pytest
from pytest import warns

def test_raises_properly():
    with pytest.raises(ValueError):
        int("x")

def test_warns_properly():
    with warns(UserWarning):
        pass

async def test_async_raises():
    with pytest.raises(KeyError):
        {}["k"]
"""

LOOPS = """async def f(driver, rows):
    for r in await driver.execute_query("MATCH (n) RETURN n LIMIT 1"):
        pass
    for a in rows:
        for b in a:
            await driver.execute_query("RETURN 1")
"""


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _findings(auditor, root: Path) -> list[tuple[str, str, int]]:
    result = auditor.scan(auditor.scope.resolve(root), root)
    return [(f.category, f.file, f.line) for f in result.findings]


def test_async_only_test_file_is_not_empty(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_async_only.py", ASYNC_ONLY)
    found = _findings(tq.TestQualityAuditor(), tmp_path)
    assert ("EMPTY_TEST_FILE", "tests/unit/test_async_only.py", 0) not in found
    assert [f for f in found if f[0] == "WEAK_TEST"] == [("WEAK_TEST", "tests/unit/test_async_only.py", 4)]


def test_pytest_raises_and_warns_count_as_assertions(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_raises.py", RAISES)
    assert _findings(tq.TestQualityAuditor(), tmp_path) == []


def test_zero_assertion_sync_test_still_weak(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_weak.py", "def test_nothing():\n    x = 1\n")
    found = _findings(tq.TestQualityAuditor(), tmp_path)
    assert found == [("WEAK_TEST", "tests/unit/test_weak.py", 1)]


def test_file_without_tests_is_still_empty(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_none.py", "def helper():\n    return 1\n")
    found = _findings(tq.TestQualityAuditor(), tmp_path)
    assert found == [("EMPTY_TEST_FILE", "tests/unit/test_none.py", 0)]


def test_n_plus_one_reported_once_per_loop_body_call(tmp_path: Path) -> None:
    _write(tmp_path, "engine/sub/loops.py", LOOPS)
    found = [f for f in _findings(qp.QueryPerformanceAuditor(), tmp_path) if f[0] == "N_PLUS_ONE"]
    assert found == [("N_PLUS_ONE", "engine/sub/loops.py", 6)]


def test_call_in_nested_loop_iterable_is_inside_the_outer_body(tmp_path: Path) -> None:
    src = (
        "async def f(driver, rows):\n"
        "    for a in rows:\n"
        "        for b in await driver.execute_query('RETURN 1'):\n"
        "            pass\n"
    )
    _write(tmp_path, "engine/sub/nested.py", src)
    found = [f for f in _findings(qp.QueryPerformanceAuditor(), tmp_path) if f[0] == "N_PLUS_ONE"]
    assert found == [("N_PLUS_ONE", "engine/sub/nested.py", 3)]


def test_call_in_nested_loop_else_is_inside_the_outer_body(tmp_path: Path) -> None:
    """A nested loop's ``else`` runs once per outer iteration; the outer loop's own ``else`` runs once."""
    src = (
        "async def f(driver, rows):\n"
        "    for row in rows:\n"
        "        for item in row:\n"
        "            pass\n"
        "        else:\n"
        "            await driver.execute_query('RETURN 1')\n"
        "    for z in rows:\n"
        "        pass\n"
        "    else:\n"
        "        await driver.execute_query('RETURN 2')\n"
    )
    _write(tmp_path, "engine/sub/nested_else.py", src)
    found = [f for f in _findings(qp.QueryPerformanceAuditor(), tmp_path) if f[0] == "N_PLUS_ONE"]
    assert found == [("N_PLUS_ONE", "engine/sub/nested_else.py", 6)]


# ── audit_dispatch --strict ─────────────────────────────────────────────────


def _run_dispatch(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["audit_dispatch.py", *argv])
    try:
        dispatch.main()
    except SystemExit as exc:
        code = exc.code
    else:
        code = 0  # main() returns normally on the non-strict path
    return code if isinstance(code, int) else (0 if code is None else 1)


def test_strict_with_no_runnable_auditor_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch, "_check_requirements", lambda _a: False)
    assert _run_dispatch(monkeypatch, "--strict") == 2


def test_non_strict_with_no_runnable_auditor_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch, "_check_requirements", lambda _a: False)
    assert _run_dispatch(monkeypatch) == 0


def test_strict_with_import_failure_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _NoFiles:
        def resolve(self, _root: Path) -> list[Path]:
            return []

    class _CleanAuditor:
        name = "clean_stub"
        requires: list[str] = []
        dependencies: list[str] = []
        scope = _NoFiles()

    saved: list[object] = []
    monkeypatch.setattr(dispatch, "IMPORT_ERRORS", {"broken_auditor": "No module named 'nope'"})
    monkeypatch.setattr(dispatch, "_resolve_auditors", lambda _args: [_CleanAuditor()])
    monkeypatch.setattr(dispatch, "_save_report", lambda results, _root: saved.append(results))
    assert _run_dispatch(monkeypatch, "--strict") == 2
    assert "broken_auditor" in capsys.readouterr().err
    assert saved, "the report is still written before the strict failure"


def test_strict_clean_run_without_import_failure_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoFiles:
        def resolve(self, _root: Path) -> list[Path]:
            return []

    class _CleanAuditor:
        name = "clean_stub"
        requires: list[str] = []
        dependencies: list[str] = []
        scope = _NoFiles()

    monkeypatch.setattr(dispatch, "IMPORT_ERRORS", {})
    monkeypatch.setattr(dispatch, "_resolve_auditors", lambda _args: [_CleanAuditor()])
    monkeypatch.setattr(dispatch, "_save_report", lambda _results, _root: None)
    assert _run_dispatch(monkeypatch, "--strict") == 0


# ── api_regression (RU-140 / RG-176) ────────────────────────────────────────


def _git(root: Path, *args: str) -> str:
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git is not None
    cmd = [git, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args]
    # Fixed git argv built from test literals inside tmp_path; no untrusted input.
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True)  # noqa: S603
    return proc.stdout


@pytest.fixture
def branched_repo(tmp_path: Path) -> Path:
    """main: engine/gone.py (class Gone) + engine/shared.py (class Shared).

    feature (HEAD) deletes gone.py and edits shared.py; after the branch point
    main adds class AddedOnMain to shared.py.
    """
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git binary not available")  # environment condition: RG-176 requires git
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "engine/gone.py", "class Gone:\n    def run(self):\n        return 1\n")
    _write(root, "engine/shared.py", "class Shared:\n    def a(self):\n        return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "engine" / "gone.py").unlink()
    _write(root, "engine/shared.py", "class Shared:\n    def a(self):\n        return 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feature work")
    _git(root, "checkout", "-q", "main")
    _write(
        root,
        "engine/shared.py",
        "class Shared:\n    def a(self):\n        return 1\n\n\nclass AddedOnMain:\n    pass\n",
    )
    _git(root, "commit", "-q", "-am", "main moves on")
    _git(root, "checkout", "-q", "feature")
    return root


def test_deleted_module_reports_class_removed(branched_repo: Path) -> None:
    from tools.auditors import api_regression as ar

    result = ar.APIRegressionAuditor().scan([], branched_repo)
    removed = [(f.category, f.file, f.message) for f in result.findings if f.category == "CLASS_REMOVED"]
    assert ("CLASS_REMOVED", "engine/gone.py", "Public class 'Gone' was removed") in removed


def test_class_added_on_main_after_branching_is_not_reported(branched_repo: Path) -> None:
    from tools.auditors import api_regression as ar

    result = ar.APIRegressionAuditor().scan([], branched_repo)
    assert not [f for f in result.findings if "AddedOnMain" in f.message]
    assert [f.file for f in result.findings] == ["engine/gone.py"]
