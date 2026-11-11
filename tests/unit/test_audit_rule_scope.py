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
the harness still exited 0. #456 put them back on ``engine/*.py``, where 16
modules that can never hold a flow (``engine/__init__.py``, ``engine/state.py``,
...) were still reported on every run, and one token anywhere in
``engine/handlers.py`` satisfied them. The TRACE rules are now ``required_all``
checks scoped to the one definition that owns each flow (``handle_match``,
``handle_sync``, ``GDSScheduler``). These tests pin each rule to that scope,
prove each required token still fires when it is removed, and pin
spec-coverage discovery, so a scope change shows up as a failing test instead
of a quietly different report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import audit_engine as ae
from tools import spec_extract as se

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
# rule id -> (anchored file, within_* key, definition name, required_all tokens)
TRACE_ANCHORS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "TRACE_MATCH_FLOW_ANCHORS": (
        "engine/handlers.py",
        "within_function",
        "handle_match",
        ("GateCompiler(", "TraversalAssembler(", "ScoringAssembler(", "execute_query("),
    ),
    "TRACE_SYNC_FLOW_ANCHORS": (
        "engine/handlers.py",
        "within_function",
        "handle_sync",
        ("SyncGenerator(", "generate_sync_query(", "execute_query("),
    ),
    "TRACE_GDS_FLOW_ANCHORS": (
        "engine/gds/scheduler.py",
        "within_class",
        "GDSScheduler",
        (
            "def register_jobs(",
            "def execute_job(",
            "def _run_louvain(",
            "def _run_cooccurrence(",
            "def _run_reinforcement(",
            "def _run_temporal_recency(",
        ),
    ),
}
TRACE_TOKEN_CASES = [
    pytest.param(rule_id, token, id=f"{rule_id}-{token}")
    for rule_id, (_, _, _, tokens) in TRACE_ANCHORS.items()
    for token in tokens
]
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


def _copy_anchor(tmp_path: Path, rel: str) -> Path:
    """A temp repo holding a byte-for-byte copy of the real anchored module."""
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _audit(root: Path, rule_id: str) -> list[ae.Finding]:
    ae.read_text.cache_clear()
    ae._list_files_cached.cache_clear()
    return ae.audit_rules(root, {"rules": [_rules()[rule_id]]})


@pytest.mark.parametrize("rule_id", sorted(TRACE_ANCHORS))
def test_trace_rule_is_scoped_to_the_owning_definition(rule_id: str) -> None:
    rel, key, name, tokens = TRACE_ANCHORS[rule_id]
    rule = _rules()[rule_id]

    assert _rel(_scope(rule)) == [rel]
    assert ae.rule_within(rule) == (key, name)
    assert tuple(rule["required_all"]) == tokens
    assert "required_any" not in rule, "a flow anchor promises every component, not any one of them"


@pytest.mark.parametrize("rule_id", sorted(TRACE_ANCHORS))
def test_trace_rule_has_no_findings_on_this_repo(rule_id: str) -> None:
    assert _audit(REPO_ROOT, rule_id) == []


@pytest.mark.parametrize(("rule_id", "token"), TRACE_TOKEN_CASES)
def test_trace_rule_fires_when_a_required_token_leaves_the_definition(tmp_path: Path, rule_id: str, token: str) -> None:
    """Rename one required call or method in the real definition only; the rest of the file keeps it."""
    rel, key, name, _ = TRACE_ANCHORS[rule_id]
    dest = _copy_anchor(tmp_path, rel)
    text = dest.read_text(encoding="utf-8")
    found = ae.definition_source(text, key, name)
    assert found is not None
    segment = found[0]
    assert token in segment
    dest.write_text(text.replace(segment, segment.replace(token, token[:-1] + "_renamed(")), encoding="utf-8")

    findings = _audit(tmp_path, rule_id)

    assert len(findings) == 1, findings
    assert findings[0].rule_id == rule_id
    assert repr(token) in findings[0].evidence
    assert f"`{name}`" in findings[0].evidence
    assert (findings[0].line_start, findings[0].line_end) == found[1:]


@pytest.mark.parametrize("rule_id", sorted(TRACE_ANCHORS))
def test_trace_rule_fires_when_the_definition_is_renamed(tmp_path: Path, rule_id: str) -> None:
    rel, key, name, _ = TRACE_ANCHORS[rule_id]
    dest = _copy_anchor(tmp_path, rel)
    keyword = "class" if key == "within_class" else "def"
    text = dest.read_text(encoding="utf-8")
    assert f"{keyword} {name}" in text
    dest.write_text(text.replace(f"{keyword} {name}", f"{keyword} {name}_renamed"), encoding="utf-8")

    findings = _audit(tmp_path, rule_id)

    kind = key.removeprefix("within_")
    assert [f.evidence for f in findings] == [f"Module-level {kind} `{name}` is not defined in this file."]


@pytest.mark.parametrize("rule_id", sorted(TRACE_ANCHORS))
def test_trace_rule_fires_when_the_anchored_module_is_gone(tmp_path: Path, rule_id: str) -> None:
    (tmp_path / "engine" / "gds").mkdir(parents=True)

    findings = _audit(tmp_path, rule_id)

    assert len(findings) == 1
    assert findings[0].evidence.startswith("Rule scope matched no files")


def test_tokens_elsewhere_in_the_file_do_not_satisfy_the_definition(tmp_path: Path) -> None:
    """The per-file rule this replaces passed when any function in the file named one component."""
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "handlers.py").write_text(
        "async def handle_match(tenant, payload):\n"
        "    return {}\n"
        "\n"
        "\n"
        "async def handle_admin(tenant, payload):\n"
        "    GateCompiler(spec); TraversalAssembler(spec); ScoringAssembler(spec)\n"
        "    return await driver.execute_query('RETURN 1')\n",
        encoding="utf-8",
    )

    findings = _audit(tmp_path, "TRACE_MATCH_FLOW_ANCHORS")

    assert len(findings) == 1
    assert (findings[0].line_start, findings[0].line_end) == (1, 2)
    assert "in function `handle_match`" in findings[0].evidence


def test_only_a_module_level_definition_is_the_anchor(tmp_path: Path) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "handlers.py").write_text(
        "def factory():\n"
        "    async def handle_match(tenant, payload):\n"
        "        GateCompiler(s); TraversalAssembler(s); ScoringAssembler(s); d.execute_query('x')\n"
        "    return handle_match\n",
        encoding="utf-8",
    )

    findings = _audit(tmp_path, "TRACE_MATCH_FLOW_ANCHORS")

    assert [f.evidence for f in findings] == ["Module-level function `handle_match` is not defined in this file."]


def test_unparseable_anchor_module_is_a_finding(tmp_path: Path) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "handlers.py").write_text("async def handle_sync(:\n", encoding="utf-8")

    findings = _audit(tmp_path, "TRACE_SYNC_FLOW_ANCHORS")

    assert len(findings) == 1
    assert findings[0].evidence.startswith("Module could not be parsed to locate function handle_sync")


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"id": "R", "within_function": "f", "within_class": "C", "required_all": ["x"]}, "set at most one of"),
        ({"id": "R", "within_function": "f", "patterns": ["x"]}, "needs required_all or required_any"),
    ],
)
def test_malformed_within_rule_is_rejected(rule: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ae.rule_within(rule)


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
