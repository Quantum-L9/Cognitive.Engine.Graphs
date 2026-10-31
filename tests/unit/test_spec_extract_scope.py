"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [audit, spec-coverage]
owner: platform
status: active
--- /L9_META ---

Unit tests for tools/spec_extract.py evidence scoping (RU-134, RG-169).

A feature's evidence must come only from the files its ``search_files``
globs declare. Before the fix ``**`` globs normalised to prefixes that
matched nothing, and the empty scope silently widened to every .py/.yaml
in the tree, so the spec YAML, tests and the extractor itself counted as
implementation evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import spec_extract as se

pytestmark = pytest.mark.unit


def _synthetic_root(root: Path) -> Path:
    (root / "engine" / "gates").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tools").mkdir()
    (root / "engine" / "gates" / "real.py").write_text("class RangeGate: pass\n", encoding="utf-8")
    (root / "tests" / "test_phantom.py").write_text("# phantom_gate is not implemented yet\n", encoding="utf-8")
    (root / "tools" / "helper.py").write_text("PHANTOM_GATE = 'phantom_gate'\n", encoding="utf-8")
    (root / "my-spec.yaml").write_text("gates:\n  types:\n    phantom_gate: {}\n", encoding="utf-8")
    return root


def test_phantom_gate_outside_scope_is_missing(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    feats = se.extract_gate_features({"gates": {"types": {"phantom_gate": {}}}})
    assert feats, "extractor produced no gate feature"

    se.scan_codebase(root, feats)

    for feat in feats:
        assert feat.status == se.Status.MISSING.value, (feat.name, feat.evidence_files)
        assert feat.evidence_files == []


def test_gate_evidence_never_comes_from_spec_tests_or_tools(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    (root / "engine" / "gates" / "types").mkdir()
    (root / "engine" / "gates" / "types" / "phantom.py").write_text("class PhantomGate: ...  # phantom_gate\n")
    feats = se.extract_gate_features({"gates": {"types": {"phantom_gate": {}}}})

    se.scan_codebase(root, feats)

    for feat in feats:
        assert feat.evidence_files == ["engine/gates/types/phantom.py"], feat.evidence_files
        assert feat.status == se.Status.PARTIAL.value


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("engine/gates/**/*.py", "engine/gates/compiler.py", True),
        ("engine/gates/**/*.py", "engine/gates/types/all_gates.py", True),
        ("engine/gates/**/*.py", "engine/scoring/assembler.py", False),
        ("engine/gates/**/*.py", "tests/unit/engine/gates/x.py", False),
        ("engine/gates/**/*.py", "engine/gates/notes.yaml", False),
        ("engine/**/*.py", "engine/handlers.py", True),
        ("domains/**/*.yaml", "domains/plasticos/spec.yaml", True),
        ("domains/**/*.yaml", "my-spec.yaml", False),
        ("engine/gds/", "engine/gds/scheduler.py", True),
        ("engine/gds/", "engine/gdsx/scheduler.py", False),
        ("engine/kge", "engine/kge/model.py", True),
        ("engine/handlers.py", "engine/handlers.py", True),
        ("engine/handlers.py", "engine/handlers_extra.py", False),
        ("engine/*.py", "engine/handlers.py", True),
        ("engine/*.py", "engine/gates/compiler.py", False),
    ],
)
def test_search_file_glob_matching(pattern: str, path: str, expected: bool) -> None:
    assert se.matches_search_file(path, pattern) is expected


def test_empty_scoped_set_does_not_fall_back_to_whole_tree(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    feat = se.SpecFeature(
        category="x",
        name="phantom",
        spec_reference="x",
        search_tokens=["phantom_gate"],
        search_files=["engine/does_not_exist/"],
    )

    se.scan_codebase(root, [feat])

    assert feat.status == se.Status.MISSING.value
    assert feat.evidence_files == []


def test_research_directory_prefixes_keep_resolving() -> None:
    """Every research search_files entry in the real tree matches at least one file."""
    repo = Path(__file__).resolve().parents[2]
    files = [str(p.relative_to(repo)) for p in (repo / "engine").rglob("*.py") if "__pycache__" not in p.parts]
    for feat in se.extract_research_features(repo):
        for pattern in feat.search_files:
            assert any(se.matches_search_file(f, pattern) for f in files), (feat.name, pattern)


def test_research_features_counted_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RG-171 (RU-135): research patterns are added to the feature list exactly once."""
    import json
    import sys

    repo = Path(__file__).resolve().parents[2]
    research = tmp_path / se.RESEARCH_DIR
    research.mkdir(parents=True)
    src = repo / se.RESEARCH_DIR / se.RESEARCH_PATTERNS_FILE
    (research / se.RESEARCH_PATTERNS_FILE).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    spec = tmp_path / "my-spec.yaml"
    spec.write_text("gates:\n  types: {}\n", encoding="utf-8")
    mapped = len(se.extract_research_features(tmp_path))
    assert mapped == 5

    monkeypatch.setattr(
        sys, "argv", ["spec_extract.py", "--spec", str(spec), "--root", str(tmp_path), "--fail-on", "NONE"]
    )
    assert se.main() == 0

    matrix = json.loads((tmp_path / "artifacts" / se.COVERAGE_MATRIX_JSON).read_text(encoding="utf-8"))
    assert matrix["categories"]["research_pattern"]["total"] == mapped
    checklist = json.loads((tmp_path / "artifacts" / "spec_checklist.json").read_text(encoding="utf-8"))
    names = [f["name"] for f in checklist if f["category"] == "research_pattern"]
    assert len(names) == len(set(names)) == mapped
