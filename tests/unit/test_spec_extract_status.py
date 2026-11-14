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

Unit tests for the evidence-based status rule of tools/spec_extract.py.

IMPLEMENTED = implementation present in its owning code AND a test under
tests/ references it; PARTIAL = implementation present, no test reference;
MISSING = no implementation. Each category is proven on a synthetic engine
tree, so the rule — not the current engine — is what these tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import spec_extract as se

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
REAL_SPEC = REPO / "graph-cognitive-engine-spec-v1.1.0.yaml"

SCHEMA = """
from enum import StrEnum


class GateType(StrEnum):
    RANGE = "range"
    THRESHOLD = "threshold"
    BOOLEAN = "boolean"


class ComputationType(StrEnum):
    GEODECAY = "geodecay"
    LOGNORMALIZED = "lognormalized"
    CUSTOMCYPHER = "customcypher"
"""

REGISTRY = """
from engine.config.schema import GateType

_REGISTRY = {
    GateType.RANGE: RangeGate,
    GateType.THRESHOLD: ThresholdGate,
}
"""

ASSEMBLER = """
from engine.config.schema import ComputationType


def compile_dimension(dim):
    dispatch = {ComputationType.GEODECAY: geo, ComputationType.LOGNORMALIZED: log}
    return dispatch[dim.computation]
"""

SCHEDULER = """
async def run(self, job_spec):
    algo = job_spec.algorithm
    if algo == "louvain":
        return await self._run_louvain(job_spec)
    elif algo == "temporalrecency":
        return await self._run_temporal_recency(job_spec)
"""

HANDLERS = """
SCORE_ACTION = "score"


async def handle_match(tenant, payload):
    return {}


async def handle_sync(tenant, payload):
    return {}


async def handle_admin(tenant, payload):
    subaction = payload["subaction"]
    if subaction == "list_domains":
        return {}
    return {}


async def handle_score_remote(tenant, payload):
    return {}


ACTION_HANDLERS = {
    "match": handle_match,
    "sync": handle_sync,
    "admin": handle_admin,
    SCORE_ACTION: handle_score_remote,
}
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def engine_root(tmp_path: Path) -> Path:
    _write(tmp_path, se.SCHEMA_FILE, SCHEMA)
    _write(tmp_path, se.GATE_REGISTRY_FILE, REGISTRY)
    _write(tmp_path, se.ASSEMBLER_FILE, ASSEMBLER)
    _write(tmp_path, se.GDS_SCHEDULER_FILE, SCHEDULER)
    _write(tmp_path, se.HANDLERS_FILE, HANDLERS)
    _write(
        tmp_path,
        "tests/unit/test_engine_behaviour.py",
        "from x import GateType, ComputationType, handle_match\n"
        "def test_a():\n"
        "    assert GateType.RANGE\n"
        "    assert ComputationType.GEODECAY\n"
        "    assert run('louvain')\n"
        "    assert handle_match\n"
        "    assert admin({'subaction': 'list_domains'})\n",
    )
    return tmp_path


def _status(root: Path, feature: se.SpecFeature) -> se.SpecFeature:
    se.scan_codebase(root, [feature])
    return feature


def _feature(category: str, name: str) -> se.SpecFeature:
    return se.SpecFeature(category=category, name=name, spec_reference=f"test:{name}")


# ── gates ──────────────────────────────────────────────────────────────────


def test_gate_registered_and_tested_is_implemented(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.GATES_CATEGORY, "range"))
    assert feat.status == se.Status.IMPLEMENTED.value
    assert feat.evidence_files == [se.SCHEMA_FILE, se.GATE_REGISTRY_FILE]
    assert feat.test_files == ["tests/unit/test_engine_behaviour.py"]


def test_gate_registered_without_test_is_partial(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.GATES_CATEGORY, "threshold"))
    assert feat.status == se.Status.PARTIAL.value
    assert feat.test_files == []


def test_gate_enum_member_without_registry_entry_is_missing(engine_root: Path) -> None:
    """A GateType member nothing registers is not an implementation."""
    feat = _status(engine_root, _feature(se.GATES_CATEGORY, "boolean"))
    assert feat.status == se.Status.MISSING.value


def test_gate_test_reference_by_registered_class_counts(engine_root: Path) -> None:
    _write(engine_root, "tests/unit/test_threshold.py", "def test_t():\n    assert ThresholdGate\n")
    feat = _status(engine_root, _feature(se.GATES_CATEGORY, "threshold"))
    assert feat.status == se.Status.IMPLEMENTED.value


# ── scoring ────────────────────────────────────────────────────────────────


def test_scoring_compiled_and_tested_is_implemented(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.SCORING_CATEGORY, "geodecay"))
    assert feat.status == se.Status.IMPLEMENTED.value
    assert feat.evidence_files == sorted([se.ASSEMBLER_FILE, se.SCHEMA_FILE])


def test_scoring_compiled_without_test_is_partial(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.SCORING_CATEGORY, "lognormalized"))
    assert feat.status == se.Status.PARTIAL.value


def test_scoring_quoted_value_in_test_counts(engine_root: Path) -> None:
    _write(engine_root, "tests/unit/test_log.py", "SPEC = {'computation': 'lognormalized'}\n")
    feat = _status(engine_root, _feature(se.SCORING_CATEGORY, "lognormalized"))
    assert feat.status == se.Status.IMPLEMENTED.value


def test_scoring_enum_member_the_assembler_never_compiles_is_missing(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.SCORING_CATEGORY, "customcypher"))
    assert feat.status == se.Status.MISSING.value


def test_scoring_name_absent_from_enum_is_missing(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.SCORING_CATEGORY, "realtime_reranking"))
    assert feat.status == se.Status.MISSING.value


# ── gds ────────────────────────────────────────────────────────────────────


def test_gds_dispatched_and_tested_is_implemented(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.GDS_CATEGORY, "louvain"))
    assert feat.status == se.Status.IMPLEMENTED.value
    assert feat.evidence_lines == [f"{se.GDS_SCHEDULER_FILE}:4"]


def test_gds_spec_name_matches_engine_literal_normalised(engine_root: Path) -> None:
    """Spec ``temporal_recency`` is the engine's ``temporalrecency``; untested, so PARTIAL."""
    feat = _status(engine_root, _feature(se.GDS_CATEGORY, "temporal_recency"))
    assert feat.status == se.Status.PARTIAL.value


def test_gds_node_similarity_follows_the_rule_not_a_special_case(engine_root: Path) -> None:
    """MISSING with no dispatch branch; IMPLEMENTED by the same rule once a branch and a test exist."""
    assert _status(engine_root, _feature(se.GDS_CATEGORY, "nodeSimilarity")).status == se.Status.MISSING.value

    _write(
        engine_root,
        se.GDS_SCHEDULER_FILE,
        SCHEDULER + '    elif algo == "nodesimilarity":\n        return await self._run_node_similarity(job_spec)\n',
    )
    assert _status(engine_root, _feature(se.GDS_CATEGORY, "nodeSimilarity")).status == se.Status.PARTIAL.value

    _write(engine_root, "tests/unit/test_similarity.py", "JOB = {'algorithm': 'nodesimilarity'}\n")
    assert _status(engine_root, _feature(se.GDS_CATEGORY, "nodeSimilarity")).status == se.Status.IMPLEMENTED.value


def test_gds_dispatch_by_dict_and_match_case_is_recognised(engine_root: Path) -> None:
    _write(
        engine_root,
        se.GDS_SCHEDULER_FILE,
        "RUNNERS = {'louvain': self._run_louvain}\n"
        "def f(algo):\n"
        "    match algo:\n"
        "        case 'cooccurrence':\n"
        "            return 1\n",
    )
    assert _status(engine_root, _feature(se.GDS_CATEGORY, "louvain")).status == se.Status.IMPLEMENTED.value
    assert _status(engine_root, _feature(se.GDS_CATEGORY, "cooccurrence")).status == se.Status.PARTIAL.value


# ── actions ────────────────────────────────────────────────────────────────


def test_action_registered_and_tested_is_implemented(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.ACTION_CATEGORY, "match"))
    assert feat.status == se.Status.IMPLEMENTED.value


def test_action_registered_without_test_is_partial(engine_root: Path) -> None:
    assert _status(engine_root, _feature(se.ACTION_CATEGORY, "sync")).status == se.Status.PARTIAL.value


def test_action_key_from_module_constant_is_resolved(engine_root: Path) -> None:
    assert _status(engine_root, _feature(se.ACTION_CATEGORY, "score")).status == se.Status.PARTIAL.value


def test_action_unregistered_is_missing(engine_root: Path) -> None:
    assert _status(engine_root, _feature(se.ACTION_CATEGORY, "outcomes")).status == se.Status.MISSING.value


def test_endpoint_realized_as_admin_subaction(engine_root: Path) -> None:
    feat = _status(engine_root, _feature(se.ACTION_CATEGORY, "domains"))
    assert feat.status == se.Status.IMPLEMENTED.value
    _write(engine_root, se.HANDLERS_FILE, HANDLERS.replace('"list_domains"', '"get_domain"'))
    assert _status(engine_root, _feature(se.ACTION_CATEGORY, "domains")).status == se.Status.MISSING.value


# ── evidence boundaries ────────────────────────────────────────────────────


def test_tests_of_the_audit_tool_are_not_feature_evidence(engine_root: Path) -> None:
    _write(
        engine_root,
        "tests/unit/test_spec_extract_fake.py",
        "from tools import spec_extract\nassert ThresholdGate\n",
    )
    assert _status(engine_root, _feature(se.GATES_CATEGORY, "threshold")).status == se.Status.PARTIAL.value


def test_absent_owning_files_mean_missing(tmp_path: Path) -> None:
    for category, name in [
        (se.GATES_CATEGORY, "range"),
        (se.SCORING_CATEGORY, "geodecay"),
        (se.GDS_CATEGORY, "louvain"),
        (se.ACTION_CATEGORY, "match"),
    ]:
        assert _status(tmp_path, _feature(category, name)).status == se.Status.MISSING.value


@pytest.mark.parametrize(
    ("rel", "text", "category"),
    [
        (se.SCHEMA_FILE, "class Other: pass\n", se.GATES_CATEGORY),
        (se.GATE_REGISTRY_FILE, "X = 1\n", se.GATES_CATEGORY),
        (se.GDS_SCHEDULER_FILE, "def f(): pass\n", se.GDS_CATEGORY),
        (se.HANDLERS_FILE, "def f(): pass\n", se.ACTION_CATEGORY),
        (se.HANDLERS_FILE, "def broken(:\n", se.ACTION_CATEGORY),
    ],
)
def test_present_owner_without_anchor_is_unresolved_not_missing(
    engine_root: Path, rel: str, text: str, category: str
) -> None:
    _write(engine_root, rel, text)
    name = {se.GATES_CATEGORY: "range", se.GDS_CATEGORY: "louvain", se.ACTION_CATEGORY: "match"}[category]
    features = [_feature(category, name)]
    with pytest.raises(se.EvidenceUnresolvedError):
        se.scan_codebase(engine_root, features)


def test_main_exits_2_on_unresolved_evidence(engine_root: Path) -> None:
    _write(engine_root, se.HANDLERS_FILE, "def f(): pass\n")
    _write(engine_root, "my-spec.yaml", "gatetypes: []\n")
    assert se.main(["--root", str(engine_root), "--spec", "my-spec.yaml", "--fail-on", "NONE"]) == 2


# ── extraction from the spec file ──────────────────────────────────────────


@pytest.fixture(scope="module")
def real_spec() -> dict:
    return se.load_yaml_file(REAL_SPEC)


def test_features_come_from_the_spec_sections(real_spec: dict) -> None:
    names = {(f.category, f.name) for f in se.collect_features(real_spec, REPO)}
    gates = {g["name"] for g in real_spec["gatetypes"]}
    scoring = {s["name"] for s in real_spec["scoringcomputationtypes"]}
    assert {n for c, n in names if c == se.GATES_CATEGORY} == gates
    assert {n for c, n in names if c == se.SCORING_CATEGORY} == scoring
    assert (se.GDS_CATEGORY, "nodeSimilarity") in names
    assert {n for c, n in names if c == se.ACTION_CATEGORY} == {
        "match",
        "sync",
        "outcomes",
        "resolve",
        "admin",
        "domains",
        "health",
        *(name for _cat, name, _src, _why in se.ENGINE_CONTRACT_FEATURES),
    }


def test_removed_entries_stay_removed(real_spec: dict) -> None:
    """SignalEvent / outcome_weighted are not in the spec; temporal_decay is only prose there."""
    features = se.collect_features(real_spec, REPO)
    names = {f.name for f in features}
    assert not names & {"SignalEvent", "outcome_weighted", "temporal_decay", "similarity", "query"}
    assert not any("detected in spec text" in f.spec_reference for f in features)
    assert {f.category for f in features} == {
        se.GATES_CATEGORY,
        se.SCORING_CATEGORY,
        se.GDS_CATEGORY,
        se.ACTION_CATEGORY,
        se.RESEARCH_CATEGORY,
    }


def test_every_feature_is_traceable(real_spec: dict) -> None:
    spec_text = REAL_SPEC.read_text(encoding="utf-8")
    contract_names = {name for _cat, name, _src, _why in se.ENGINE_CONTRACT_FEATURES}
    for feature in se.collect_features(real_spec, REPO):
        if feature.category == se.RESEARCH_CATEGORY:
            assert feature.spec_reference.startswith(f"{se.RESEARCH_DIR}/{se.RESEARCH_PATTERNS_FILE}#")
        elif feature.name in contract_names:
            assert feature.spec_reference.startswith("engine contract: ")
        else:
            assert feature.name in spec_text, feature


def test_engine_contract_features_carry_a_reason() -> None:
    for entry in se.ENGINE_CONTRACT_FEATURES:
        assert all(entry), entry


def test_real_repo_node_similarity_status_tracks_the_scheduler(real_spec: dict) -> None:
    """The real GDS similarity gap is reported MISSING exactly while the scheduler has no branch for it."""
    feature = next(f for f in se.collect_features(real_spec, REPO) if f.name == "nodeSimilarity")
    se.scan_codebase(REPO, [feature])
    # The same sources scan_codebase reads, so a dispatch through an imported
    # constant (``algo in NODE_SIMILARITY_ALGORITHMS``) resolves here too.
    index = se.EngineIndex(se._read_tree(REPO, ".py", (".venv", "__pycache__")))
    dispatched = "nodesimilarity" in index.gds_dispatch()
    assert (feature.status == se.Status.MISSING.value) is not dispatched


def test_report_header_states_the_rule(engine_root: Path) -> None:
    _write(engine_root, "my-spec.yaml", "gatetypes:\n  - name: range\n")
    assert se.main(["--root", str(engine_root), "--spec", "my-spec.yaml", "--fail-on", "NONE"]) == 0
    report = (engine_root / "artifacts" / "coverage_report.md").read_text(encoding="utf-8")
    assert se.STATUS_DEFINITIONS in report
    checklist = json.loads((engine_root / "artifacts" / "spec_checklist.json").read_text(encoding="utf-8"))
    assert checklist[0]["test_files"] == ["tests/unit/test_engine_behaviour.py"]


# ── Test evidence ignores non-executable text (Codex P2 on #486) ─────────────


def test_executable_source_blanks_comments_docstrings_and_skip_reasons() -> None:
    source = '''"""Module docstring naming ThresholdGate."""
import pytest

# TODO: test ThresholdGate


def test_other() -> None:
    """Docstring naming ThresholdGate."""
    "inert string naming ThresholdGate"
    assert True


@pytest.mark.skip(reason="ThresholdGate not ready")
def test_skipped() -> None:
    pass
'''
    text = se._executable_source(source)
    assert "ThresholdGate" not in text
    assert len(text) == len(source)
    assert text.count("\n") == source.count("\n")


def test_executable_source_keeps_strings_that_code_uses() -> None:
    source = 'spec = {"algorithm": "nodeSimilarity"}\nresult = run(GateType.THRESHOLD)  # ThresholdGate\n'
    text = se._executable_source(source)
    assert '"nodeSimilarity"' in text
    assert "GateType.THRESHOLD" in text
    assert "ThresholdGate" not in text


def test_unparseable_source_still_drops_comments() -> None:
    source = "def broken(:\n    x = 1  # ThresholdGate\n"
    assert "ThresholdGate" not in se._executable_source(source)


def test_gds_dispatch_resolves_a_named_set_imported_from_another_module() -> None:
    """``algo in NAMED_SET`` counts as dispatch: the probe resolves the set to its members."""
    sources = {
        se.GDS_SCHEDULER_FILE: (
            "from engine.gds.extra import EXTRA_ALGORITHMS\n"
            "async def run(algo):\n"
            "    if algo == 'louvain':\n"
            "        return 1\n"
            "    elif algo in EXTRA_ALGORITHMS:\n"
            "        return 2\n"
        ),
        "engine/gds/extra.py": (
            "CANONICAL = 'nodeSimilarity'\n"
            "ALIASES: frozenset[str] = frozenset({'node_similarity'})\n"
            "EXTRA_ALGORITHMS: frozenset[str] = frozenset({CANONICAL}) | ALIASES\n"
        ),
    }
    dispatch = se.EngineIndex(sources).gds_dispatch()
    assert {"louvain", "nodesimilarity"} <= set(dispatch)
    assert dispatch["nodesimilarity"][0] in {"nodeSimilarity", "node_similarity"}
    assert dispatch["nodesimilarity"][1] == 5  # the dispatch line, not the constant's line


def test_gds_dispatch_ignores_an_unresolvable_name() -> None:
    sources = {
        se.GDS_SCHEDULER_FILE: (
            "async def run(algo):\n"
            "    if algo == 'louvain':\n"
            "        return 1\n"
            "    elif algo in SOMEWHERE_ELSE:\n"
            "        return 2\n"
        )
    }
    assert set(se.EngineIndex(sources).gds_dispatch()) == {"louvain"}
