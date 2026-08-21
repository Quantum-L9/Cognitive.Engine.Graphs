#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tools, governance]
tags: [precommit, pytest, unit]
owner: platform
status: active
--- /L9_META ---

Select unit tests for the local pytest-unit pre-commit hook.

Commits receive only tests that cover the staged Python files. The full
catalog stays on `make test` / CI pytest. `pre-commit run --all-files`
(CI hook job) still runs the historical unit subset when many files arrive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SLOW_UNLESS_STAGED: frozenset[str] = frozenset(
    {
        "tests/unit/test_gates_all_types.py",
        "tests/unit/test_scoring.py",
        "tests/unit/test_config.py",
        "tests/unit/test_arbitration.py",
        "tests/unit/test_wave6_dormant_features.py",
    }
)

NON_UNIT_PREFIXES: tuple[str, ...] = (
    "tests/integration/",
    "tests/compliance/",
    "tests/e2e/",
    "tests/performance/",
    "tests/invariants/",
)

ALL_FILES_THRESHOLD = 40

LEGACY_ARGV: tuple[str, ...] = (
    "-m",
    "unit",
    "--ignore=tests/unit/test_gates_all_types.py",
    "--ignore=tests/unit/test_scoring.py",
    "--ignore=tests/unit/test_config.py",
    "--ignore=tests/unit/test_arbitration.py",
    "--ignore=tests/unit/test_wave6_dormant_features.py",
)


def _norm(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def infer_unit_tests(impl: str, repo: Path) -> list[str]:
    stem = Path(impl).stem
    parent = Path(impl).parent.name
    candidates = [
        f"tests/unit/test_{stem}.py",
        f"tests/unit/test_{parent}_{stem}.py",
        f"tests/unit/test_{parent}.py",
    ]
    found = [item for item in candidates if (repo / item).is_file()]
    if found:
        return found
    unit_dir = repo / "tests" / "unit"
    if not unit_dir.is_dir():
        return []
    prefix = "test_"
    suffix = f"_{stem}.py"
    hits: list[str] = []
    for path in unit_dir.glob(f"test_*_{stem}.py"):
        if not path.is_file():
            continue
        mid = path.name[len(prefix) : -len(suffix)]
        if mid and "_" not in mid:
            hits.append(path.relative_to(repo).as_posix())
    return sorted(hits)[:8]


def select_unit_tests(changed: list[str], repo: Path) -> list[str]:
    py_files = [_norm(path) for path in changed if _norm(path).endswith(".py")]
    selected: list[str] = []
    for path in py_files:
        if any(path.startswith(prefix) for prefix in NON_UNIT_PREFIXES):
            continue
        if path.startswith("tests/"):
            selected.append(path)
            continue
        selected.extend(infer_unit_tests(path, repo))

    staged = set(py_files)
    unique: list[str] = []
    seen: set[str] = set()
    for item in selected:
        if item in seen:
            continue
        if item in SLOW_UNLESS_STAGED and item not in staged:
            continue
        if not (repo / item).is_file():
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _run_pytest(args: list[str]) -> int:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{existing}" if existing else str(REPO_ROOT)
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", *args]
    print("pytest-unit:", " ".join(cmd[3:]))
    completed = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    files = [_norm(item) for item in (argv if argv is not None else sys.argv[1:])]
    py_files = [item for item in files if item.endswith(".py")]
    if not py_files:
        print("OK: no Python files staged; skip pytest-unit (full catalog is make test)")
        return 0
    if len(py_files) >= ALL_FILES_THRESHOLD:
        print("OK: many files — historical unit subset (CI --all-files)")
        return _run_pytest(["tests/", *LEGACY_ARGV])
    selected = select_unit_tests(py_files, REPO_ROOT)
    if not selected:
        print("OK: no unit tests inferred for staged Python; full catalog is make test")
        return 0
    return _run_pytest(selected)


if __name__ == "__main__":
    raise SystemExit(main())
