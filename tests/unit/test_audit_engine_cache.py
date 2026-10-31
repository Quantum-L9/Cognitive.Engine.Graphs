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


# ── RU-132 / RG-167: recursive globs and the C-009 label check ──────────────


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo_rules() -> dict:
    return ae.load_yaml(REPO_ROOT / "tools" / "audit_rules.yaml")


def _rule(rule_id: str) -> dict:
    return {"rules": [r for r in _repo_rules()["rules"] if r["id"] == rule_id]}


def _clear() -> None:
    ae.read_text.cache_clear()
    ae._list_files_cached.cache_clear()


@pytest.mark.unit
def test_list_files_recurses_double_star(tmp_path: Path) -> None:
    for rel in ("engine/a.py", "engine/gates/b.py", "engine/gates/types/c.py", "other/d.py"):
        _write(tmp_path, rel, "x = 1\n")
    _clear()

    result = ae.list_files(tmp_path, ["engine/**/*.py"], [])

    assert [p.relative_to(tmp_path).as_posix() for p in result] == [
        "engine/a.py",
        "engine/gates/b.py",
        "engine/gates/types/c.py",
    ]


@pytest.mark.unit
def test_exclude_double_star(tmp_path: Path) -> None:
    for rel in ("engine/a.py", "engine/legacy/b.py", "engine/legacy/deep/c.py"):
        _write(tmp_path, rel, "x = 1\n")
    _clear()

    result = ae.list_files(tmp_path, ["engine/**/*.py"], ["engine/legacy/**/*.py"])

    assert [p.relative_to(tmp_path).as_posix() for p in result] == ["engine/a.py"]


@pytest.mark.unit
def test_list_files_covers_every_engine_module() -> None:
    _clear()
    expected = sorted(p for p in (REPO_ROOT / "engine").rglob("*.py") if p.is_file())
    assert ae.list_files(REPO_ROOT, ["engine/**/*.py"], []) == expected


@pytest.mark.unit
def test_nested_fastapi_import_is_critical(tmp_path: Path) -> None:
    _write(tmp_path, "engine/gates/x.py", "from fastapi import APIRouter\n")
    _write(tmp_path, "engine/handlers.py", "from fastapi import APIRouter\n")
    _clear()

    findings = ae.audit_rules(tmp_path, _rule("ARCH_NO_FASTAPI_IN_ENGINE"))

    assert [(f.severity, Path(f.file).relative_to(tmp_path).as_posix()) for f in findings] == [
        ("CRITICAL", "engine/gates/x.py")
    ]


SANITIZED_ELSEWHERE = """from engine.utils.security import sanitize_label


async def query(driver, spec):
    sanitize_label(spec.other_label)
    cypher = f"MATCH (n:{spec.target_node}) RETURN n"
    return await driver.execute_query(cypher)
"""

# engine/compliance/pii.py shape: an f-string with braces, then (many lines
# later) a plain Cypher string. Neither line interpolates a label.
CROSS_LINE = """def path_of(prefix, key):
    return f"{prefix}.{key}"


QUERY = "MATCH (n:Person {id: $id}) RETURN n"
"""


@pytest.mark.unit
def test_sec_label_no_cross_line_or_whole_file_exemption(tmp_path: Path) -> None:
    _write(tmp_path, "engine/sub/x.py", SANITIZED_ELSEWHERE)
    _write(tmp_path, "engine/compliance/pii.py", CROSS_LINE)
    _clear()

    findings = ae.audit_rules(tmp_path, _rule("SEC_LABEL_SANITIZATION_REQUIRED"))

    located = [(f.severity, f.rule_id, Path(f.file).relative_to(tmp_path).as_posix()) for f in findings]
    assert located == [("CRITICAL", "SEC_LABEL_SANITIZATION_REQUIRED", "engine/sub/x.py")]
    assert findings[0].line_start is not None
    assert findings[0].line_start <= 6 <= (findings[0].line_end or 0)
    assert "spec.target_node" in findings[0].evidence


@pytest.mark.unit
def test_sec_label_waived_cypher_lint_finding_is_not_reported(tmp_path: Path) -> None:
    src = (
        'def q(spec):\n    return f"MATCH (n:{spec.label}) RETURN n"  # cypher-lint: allow label from validated spec\n'
    )
    _write(tmp_path, "engine/sub/w.py", src)
    _clear()

    assert ae.audit_rules(tmp_path, _rule("SEC_LABEL_SANITIZATION_REQUIRED")) == []


@pytest.mark.unit
def test_sec_label_unparsable_module_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "engine/sub/broken.py", "def f(:\n")
    _clear()

    findings = ae.audit_rules(tmp_path, _rule("SEC_LABEL_SANITIZATION_REQUIRED"))

    assert [f.severity for f in findings] == ["CRITICAL"]


@pytest.mark.unit
def test_current_tree_has_no_critical_or_high_findings() -> None:
    _clear()
    findings = ae.audit_rules(REPO_ROOT, _repo_rules())
    blocking = [(f.severity, f.rule_id, f.file, f.line_start) for f in findings if f.severity in {"CRITICAL", "HIGH"}]
    assert blocking == []
