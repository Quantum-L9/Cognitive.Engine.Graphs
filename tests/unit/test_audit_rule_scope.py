"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [audit, spec-coverage, regression]
owner: platform
status: active
--- /L9_META ---

Regression guard: audit-rule and spec-coverage scope, evaluated on this repo.

RU-132 (#353) made ``**`` in ``tools/audit_engine.py`` globs recursive. That
was right for the CRITICAL/HIGH rules, which until then only scanned the
top-level ``engine/*.py`` modules, but the two per-file TRACE_*_FLOW_ANCHORS
rules shared the same ``engine/**/*.py`` glob and silently widened from the
lifecycle modules to every engine module: 17 MEDIUM findings became 233 while
the harness still exited 0. These tests pin each rule to the scope it is
written for, and pin spec-coverage discovery, so a scope change shows up as a
failing test instead of a quietly different report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import audit_engine as ae
from tools import spec_extract as se

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_PER_FILE_RULES = ("TRACE_MATCH_FLOW_ANCHORS", "TRACE_SYNC_FLOW_ANCHORS")
# Rules that forbid something anywhere in the engine must keep scanning every
# engine module (RU-132); narrowing them would hide nested violations.
ENGINE_WIDE_RULES = (
    "ARCH_NO_FASTAPI_IN_ENGINE",
    "ARCH_NO_TENANT_RESOLUTION",
    "SEC_LABEL_SANITIZATION_REQUIRED",
)
EXPECTED_SPEC = "graph-cognitive-engine-spec-v1.1.0.yaml"


def _rules() -> dict[str, dict]:
    return {r["id"]: r for r in ae.load_yaml(REPO_ROOT / "tools" / "audit_rules.yaml")["rules"]}


def _scope(rule: dict, root: Path = REPO_ROOT) -> list[Path]:
    ae.read_text.cache_clear()
    ae._list_files_cached.cache_clear()
    return ae.list_files(root, rule.get("include_globs", []), rule.get("exclude_globs", []))


def _rel(paths: list[Path], root: Path = REPO_ROOT) -> list[str]:
    return [p.relative_to(root).as_posix() for p in paths]


# ── audit rules ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rule_id", TRACE_PER_FILE_RULES)
def test_trace_rule_scope_is_the_top_level_lifecycle_modules(rule_id: str) -> None:
    expected = sorted(p for p in (REPO_ROOT / "engine").glob("*.py") if p.is_file())

    scope = _scope(_rules()[rule_id])

    assert scope == expected
    assert "engine/handlers.py" in _rel(scope)
    assert all(p.parent == REPO_ROOT / "engine" for p in scope), _rel(scope)


@pytest.mark.parametrize("rule_id", TRACE_PER_FILE_RULES)
def test_trace_rule_ignores_subpackage_modules(tmp_path: Path, rule_id: str) -> None:
    (tmp_path / "engine" / "arbitration").mkdir(parents=True)
    (tmp_path / "engine" / "handlers.py").write_text("async def handle_match(): ...\nasync def handle_sync(): ...\n")
    (tmp_path / "engine" / "state.py").write_text("STATE = None\n")
    (tmp_path / "engine" / "arbitration" / "engine.py").write_text("x = 1\n")
    ae.read_text.cache_clear()
    ae._list_files_cached.cache_clear()

    findings = ae.audit_rules(tmp_path, {"rules": [_rules()[rule_id]]})

    assert [(f.rule_id, Path(f.file).relative_to(tmp_path).as_posix()) for f in findings] == [
        (rule_id, "engine/state.py")
    ]


def test_trace_findings_on_this_repo_stay_on_lifecycle_modules() -> None:
    rules = _rules()
    ae.read_text.cache_clear()
    ae._list_files_cached.cache_clear()

    findings = ae.audit_rules(REPO_ROOT, {"rules": [rules[r] for r in TRACE_PER_FILE_RULES]})

    flagged = {Path(f.file).resolve() for f in findings}
    assert all(p.parent == REPO_ROOT / "engine" for p in flagged), sorted(map(str, flagged))
    assert REPO_ROOT / "engine" / "handlers.py" not in flagged


@pytest.mark.parametrize("rule_id", ENGINE_WIDE_RULES)
def test_engine_wide_rules_still_scan_nested_modules(rule_id: str) -> None:
    rule = _rules()[rule_id]
    excluded = {REPO_ROOT / e for e in rule.get("exclude_globs", [])}
    expected = sorted(p for p in (REPO_ROOT / "engine").rglob("*.py") if p.is_file() and p not in excluded)

    assert _scope(rule) == expected


def test_every_globbed_rule_resolves_to_files() -> None:
    """A rule whose globs match nothing passes vacuously; that is a scoping bug, not a clean audit."""
    empty = [rule_id for rule_id, rule in _rules().items() if rule.get("include_globs") and not _scope(rule)]
    assert empty == []


# ── spec coverage ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def scanned_features() -> list[se.SpecFeature]:
    candidates = se.spec_candidates(REPO_ROOT)
    assert [p.name for p in candidates] == [EXPECTED_SPEC]
    features = se.collect_features(se.load_yaml_file(candidates[0]), REPO_ROOT)
    se.scan_codebase(REPO_ROOT, features)
    return features


def test_spec_features_are_discovered_once(scanned_features: list[se.SpecFeature]) -> None:
    keys = [(f.category, f.name) for f in scanned_features]
    assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)
    categories = {f.category for f in scanned_features}
    assert {"gates", "scoring", "action_handler", "gds_algorithm", se.RESEARCH_CATEGORY} <= categories


def test_every_feature_scope_resolves_to_files(scanned_features: list[se.SpecFeature]) -> None:
    """MISSING must mean "token absent from the declared files", never "the declared files matched nothing"."""
    files = [
        p.relative_to(REPO_ROOT).as_posix()
        for pattern in ("**/*.py", "**/*.yaml")
        for p in REPO_ROOT.glob(pattern)
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    ]
    unresolved = [
        (f.category, f.name, pattern)
        for f in scanned_features
        for pattern in f.search_files
        if not any(se.matches_search_file(path, pattern) for path in files)
    ]
    assert unresolved == []


def test_evidence_comes_only_from_declared_scope(scanned_features: list[se.SpecFeature]) -> None:
    stray = [
        (f.category, f.name, path)
        for f in scanned_features
        if f.search_files
        for path in f.evidence_files
        if not any(se.matches_search_file(path, pattern) for pattern in f.search_files)
    ]
    assert stray == []


def test_every_spec_gate_is_found_in_the_gate_package(scanned_features: list[se.SpecFeature]) -> None:
    gates = [f for f in scanned_features if f.category == "gates"]
    assert gates
    assert [f.name for f in gates if f.status != se.Status.IMPLEMENTED.value] == []
