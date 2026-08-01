"""Unit tests — audit_engine per-run caching and coverage_matrix.json fix.

Covers the throughput hardening in ``tools/audit_engine.py``:

* ``read_text`` and ``_list_files_cached`` memoize within a single audit run so
  files matched by overlapping rule globs (many rules share ``engine/**/*.py``)
  are read and walked once instead of once per rule.
* The audit engine no longer writes ``artifacts/coverage_matrix.json`` — that
  file is owned by ``tools/spec_extract.py`` and the previous shared write only
  looked harmless because the harness ran the two steps serially.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import audit_engine as ae  # noqa: E402


@pytest.mark.unit
def test_read_text_is_cached_within_a_run(tmp_path: Path) -> None:
    f = tmp_path / "sample.py"
    f.write_text("original", encoding="utf-8")
    ae.read_text.cache_clear()

    first = ae.read_text(f)
    # A single-pass audit reuses the first read; mutating on disk must not leak
    # a second, inconsistent view into the same run.
    f.write_text("changed", encoding="utf-8")
    second = ae.read_text(f)

    assert first == "original"
    assert second == "original"
    info = ae.read_text.cache_info()
    assert info.misses >= 1
    assert info.hits >= 1


@pytest.mark.unit
def test_list_files_cached_sorted_and_stable(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    ae._list_files_cached.cache_clear()

    r1 = ae.list_files(tmp_path, ["*.py"], [])
    r2 = ae.list_files(tmp_path, ["*.py"], [])

    assert r1 == r2 == sorted(r1)
    assert [p.name for p in r1] == ["a.py", "b.py"]
    # The second identical glob set is served from cache — this is the redundant
    # directory walk that overlapping rules used to repeat.
    assert ae._list_files_cached.cache_info().hits >= 1


@pytest.mark.unit
def test_list_files_respects_excludes(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "skip.py").write_text("y", encoding="utf-8")
    ae._list_files_cached.cache_clear()

    result = ae.list_files(tmp_path, ["*.py"], ["skip.py"])

    assert [p.name for p in result] == ["keep.py"]


@pytest.mark.unit
def test_audit_rules_deterministic_with_shared_glob(tmp_path: Path) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "mod.py").write_text("import fastapi\n", encoding="utf-8")
    rules = {
        "rules": [
            {
                "id": "R1",
                "severity": "HIGH",
                "description": "no fastapi",
                "include_globs": ["engine/**/*.py"],
                "patterns": ["import fastapi"],
            },
            {
                "id": "R2",
                "severity": "MEDIUM",
                "description": "no fastapi (again)",
                "include_globs": ["engine/**/*.py"],
                "patterns": ["import fastapi"],
            },
        ]
    }
    ae.read_text.cache_clear()
    ae._list_files_cached.cache_clear()

    def key(findings: list[ae.Finding]) -> list[tuple[str, str, str]]:
        return sorted((f.rule_id, f.file, f.evidence) for f in findings)

    first = ae.audit_rules(tmp_path, rules)
    second = ae.audit_rules(tmp_path, rules)

    assert key(first) == key(second)
    assert {f.rule_id for f in first} == {"R1", "R2"}
    # The two rules share ``engine/**/*.py`` — the walk is resolved once.
    assert ae._list_files_cached.cache_info().hits >= 1


@pytest.mark.unit
def test_audit_engine_does_not_own_coverage_matrix() -> None:
    # Regression guard for the collision fix: coverage_matrix.json belongs to
    # spec_extract.py. audit_engine must not define a writer for it.
    assert not hasattr(ae, "write_coverage")
