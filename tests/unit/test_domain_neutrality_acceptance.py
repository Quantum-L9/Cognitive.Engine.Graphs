"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-neutrality, acceptance]
owner: engine-team
status: active
--- /L9_META ---

CEG domain-neutrality Stage 2 — bounded red/green acceptance proof (ADR-112).

A downstream operator who has never heard of PlasticOS or IdeaOS installs the
core, points it at a valid authorized domain pack, and gets the same general
graph capabilities without editing engine source. The two packs under
``tests/fixtures/domains`` share no vocabulary with any shipped pack, so a test
that passes here proves the engine executed the domain's declared contract
rather than a built-in recipe. They are verification inputs, not products.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from engine.config.loader import DomainNotFoundError, DomainPackLoader
from engine.config.schema import DomainSpec
from engine.gates.compiler import GateCompiler
from engine.gds.scheduler import GDSScheduler
from engine.scoring.assembler import ScoringAssembler
from engine.sync.generator import SyncGenerator
from engine.traversal.assembler import TraversalAssembler
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT, SYNTHETIC_VOCABULARY, synthetic_loader

pytestmark = pytest.mark.unit

# Vocabulary the former built-in recipes used. None of it may appear in what
# the engine generates for a domain that never declared it.
_FORMER_BUILT_INS = (
    "Facility",
    "TRANSACTED_WITH",
    "RECENTLY_TRANSACTED_WITH",
    "ACCEPTED_MATERIAL_FROM",
    "REJECTED_MATERIAL_FROM",
    "EquipmentType",
    "HAS_EQUIPMENT",
    "closed_won",
    "plasticos",
    "supply_opportunity_to_buyer_facility",
    # Stage 2 removal (owner decisions on items 5 and 8): the outcome node,
    # exclusion edge and community properties were borrowed defaults.
    "TransactionOutcome",
    "RESULTED_IN",
    "EXCLUDED_FROM",
    "community_id",
    "communityId",
)

# Former geo defaults. As substrings they occur in generic Cypher and words
# ("relationship", "translate", "long", "latest"), so they are matched as whole
# identifiers only: `candidate.lat`, `$lon` and `{lat_property}` default names.
_FORMER_BUILT_IN_IDENTIFIERS = ("lat", "lon")


def _assert_free_of_former_built_ins(text: str, *, allowed: tuple[str, ...] = ()) -> None:
    for token in _FORMER_BUILT_INS:
        if token in allowed:
            continue
        assert token not in text, f"generic path emitted former built-in vocabulary {token!r}: {text[:300]}"
    for token in _FORMER_BUILT_IN_IDENTIFIERS:
        if token in allowed:
            continue
        assert not re.search(rf"(?<![A-Za-z0-9_]){token}(?![A-Za-z0-9_])", text), (
            f"generic path emitted former built-in identifier {token!r}: {text[:300]}"
        )


@pytest.fixture(params=sorted(SYNTHETIC_VOCABULARY), ids=str)
def synthetic(request: pytest.FixtureRequest) -> tuple[str, DomainSpec]:
    return request.param, synthetic_loader().load_domain(request.param)


# ── Core startup without any named domain ───────────────────────────────────


def test_core_imports_without_any_domain_pack() -> None:
    """The engine's executable modules import with no pack on disk at all."""
    modules = {
        name: importlib.import_module(name)
        for name in (
            "engine.boot",
            "engine.gds.scheduler",
            "engine.handlers",
            "engine.inference_rule_registry",
            "engine.models.payloads",
        )
    }
    assert modules["engine.boot"].GraphLifecycle
    assert modules["engine.handlers"].ACTION_HANDLERS


@pytest.mark.asyncio
async def test_core_startup_with_empty_domain_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup with an empty optional domain directory: no named-domain dependency, readiness semantics intact."""
    from engine import boot
    from engine.config.settings import settings
    from engine.state import _reset_singleton, get_state

    monkeypatch.setattr(settings, "domains_root", tmp_path)
    monkeypatch.setattr(settings, "sdk_participation_enabled", True)
    monkeypatch.setattr(settings, "postgres_dsn", None)

    fake_driver = MagicMock()
    fake_driver.connect = AsyncMock()
    fake_driver.close = AsyncMock()
    _reset_singleton()
    lifecycle = boot.GraphLifecycle()
    with patch.object(boot, "GraphDriver", return_value=fake_driver):
        await lifecycle.startup()
        try:
            assert lifecycle._domain_loader is not None
            assert lifecycle._domain_loader.list_domains() == []
            assert lifecycle._schedulers == []
            assert get_state().is_initialized
        finally:
            await lifecycle.shutdown()
            _reset_singleton()


def test_semantic_registry_allowlist_stays_retired() -> None:
    """A11: the CANONICAL_ENTITY_TYPES allowlist was removed; no engine code may define or read it again.

    Prose (the semantic registry's own removal note) may name it; code may not.
    """
    from engine.config import semantic_registry

    assert not hasattr(semantic_registry, "CANONICAL_ENTITY_TYPES")
    engine_root = Path(__file__).resolve().parents[2] / "engine"
    offenders: list[str] = []
    for path in sorted(engine_root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
            if name == "CANONICAL_ENTITY_TYPES" or (
                isinstance(node, ast.alias) and "CANONICAL_ENTITY_TYPES" in (node.name, node.asname)
            ):
                offenders.append(f"{path.relative_to(engine_root.parent)}:{getattr(node, 'lineno', '?')}")
    assert offenders == [], offenders


# ── Domain admission ────────────────────────────────────────────────────────


def test_unknown_but_valid_authorized_domains_are_accepted() -> None:
    loader = synthetic_loader()
    assert loader.list_domains() == sorted(SYNTHETIC_VOCABULARY)
    for domain_id in SYNTHETIC_VOCABULARY:
        assert loader.load_domain(domain_id).domain.id == domain_id


def test_missing_or_unadmitted_domain_is_rejected(tmp_path: Path) -> None:
    loader = synthetic_loader()
    with pytest.raises(DomainNotFoundError, match="not found"):
        loader.load_domain("plasticos")
    with pytest.raises(DomainNotFoundError, match="not found"):
        loader.load_domain("does-not-exist")
    # A pack with a gate that cannot compile is withheld, never served. A scalar
    # (`queryparam: 90`) is a literal operand since CEG-009b; a parameter name
    # that is not an identifier is still refused by the compiler (C-009). The
    # name is renamed in the queryschema too, so the gate references a declared
    # parameter (RU-050 rejects undeclared ones at validation) and the refusal
    # under test is the compiler's.
    broken = tmp_path / "broken-domain"
    broken.mkdir()
    raw = (SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text()
    raw = (
        raw.replace("id: cheese-aging", "id: broken-domain")
        .replace("queryparam: max_turnover_days", 'queryparam: "max turnover days"')
        .replace("- name: max_turnover_days", '- name: "max turnover days"')
    )
    (broken / "spec.yaml").write_text(raw)
    with pytest.raises(DomainNotFoundError, match="do not compile"):
        DomainPackLoader(config_path=str(tmp_path)).load_domain("broken-domain")


# ── Labels, edge types and directions come from declared configuration ──────


def test_gates_traversal_scoring_and_sync_compile_from_declared_vocabulary(synthetic: tuple[str, DomainSpec]) -> None:
    domain_id, spec = synthetic
    direction = SYNTHETIC_VOCABULARY[domain_id]["direction"]

    where = GateCompiler(spec).compile_all_gates(direction)
    traversal = "\n".join(TraversalAssembler(spec).assemble_traversal(direction))
    scoring, _ = ScoringAssembler(spec).assemble_scoring_clause(direction, {})
    assert spec.sync is not None
    sync = "\n".join(SyncGenerator(spec).generate_sync_query(endpoint, []) for endpoint in spec.sync.endpoints)

    for gate in spec.gates:
        assert gate.candidateprop in where
    for dim in spec.scoring.dimensions:
        assert dim.candidateprop in scoring
    for endpoint in spec.sync.endpoints:
        assert f":{endpoint.targetnode} " in sync
    _assert_free_of_former_built_ins(where + traversal + scoring + sync)


def test_unsupported_match_direction_is_rejected(synthetic: tuple[str, DomainSpec]) -> None:
    _domain_id, spec = synthetic
    candidate_labels = [c.label for c in spec.matchentities.candidate if c.matchdirection == "buyer_to_seller"]
    assert candidate_labels == []  # handle_match raises ValidationError on exactly this condition
    assert "supply_opportunity_to_buyer_facility" not in spec.queryschema.matchdirections


# ── GDS: declared job vocabulary or explicit failure ────────────────────────


@pytest.mark.asyncio
async def test_gds_jobs_run_over_declared_vocabulary_only() -> None:
    spec = synthetic_loader().load_domain("orbital-logistics")
    driver = MagicMock()
    driver.execute_query = AsyncMock(
        return_value=[{"accepted_edges": 1, "rejected_edges": 1, "edges_created": 1, "edges_pruned": 0}]
    )
    scheduler = GDSScheduler(spec, driver)

    for job in spec.gdsjobs:
        result = await scheduler.execute_job(job)
        assert result["status"] == "success", result

    sent = "\n".join(str(call.args[0]) for call in driver.execute_query.call_args_list)
    assert ":OrbitalDepot" in sent
    assert ":RESUPPLIED_BY" in sent
    assert ":TRUSTED_SUPPLIER" in sent
    assert ":UNRELIABLE_SUPPLIER" in sent
    assert ":RECENTLY_RESUPPLIED_BY" in sent
    assert "t.docked_at" in sent
    _assert_free_of_former_built_ins(sent)


@pytest.mark.asyncio
async def test_gds_job_with_missing_domain_mappings_fails_explicitly() -> None:
    """Never an implicit recipe: the job reports exactly what the pack must declare."""
    from engine.config.schema import GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec

    spec = synthetic_loader().load_domain("cheese-aging")
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    scheduler = GDSScheduler(spec, driver)
    bare = GDSJobSpec(
        name="bare_reinforcement",
        algorithm="reinforcement",
        schedule=GDSJobScheduleSpec(type="manual"),
        projection=GDSProjectionSpec(nodelabels=["AgingCave"], edgetypes=["SHARES_CULTURE_WITH"]),
    )
    result = await scheduler.execute_job(bare)
    assert result["status"] == "failed"
    assert "requires 'sourceedge'" in result["error"]
    driver.execute_query.assert_not_called()


# ── Inference: executes only when authorized and supplied ───────────────────


def _bind_handlers(spec: DomainSpec) -> tuple[MagicMock, list[tuple[str, dict[str, Any]]]]:
    from engine.handlers import init_dependencies
    from engine.state import get_state

    calls: list[tuple[str, dict[str, Any]]] = []

    async def _record(*, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        calls.append((cypher, parameters))
        return [{"outcome_id": "out_x"}]

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=_record)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    get_state().reset()
    init_dependencies(driver, loader)
    return driver, calls


@pytest.mark.asyncio
async def test_inference_feedback_runs_only_domain_authorized_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.handlers import handle_admin
    from engine.state import _reset_singleton

    monkeypatch.setattr(settings, "graph_inference_feedback_enabled", True)
    spec = synthetic_loader().load_domain("orbital-logistics")  # declares no plugins.inference_rules
    _bind_handlers(spec)
    try:
        with pytest.raises(Exception, match="not authorized by domain"):
            await handle_admin(
                "orbital-logistics",
                {
                    "subaction": "emit_inference_feedback",
                    "entity": {"employee_count": 40},
                    "entity_id": "depot-1",
                    "rules": ["infer_company_size_tier"],
                },
            )
        with patch("engine.gate_egress.emit_graph_inference_result", AsyncMock(return_value={"status": "ok"})):
            result = await handle_admin(
                "orbital-logistics",
                {"subaction": "emit_inference_feedback", "entity": {"employee_count": 40}, "entity_id": "depot-1"},
            )
        assert result["rules_run"] == []
        assert result["results_produced"] == 0
    finally:
        _reset_singleton()


def test_unsupported_business_inference_is_never_synthesized() -> None:
    from engine.inference_rule_registry import InferenceContext, execute_rule, list_registered_rules

    registered = list_registered_rules()
    for vertical_rule in (
        "infer_material_grade_from_mfi",
        "infer_facility_tier_from_capacity",
        "infer_contamination_tolerance",
    ):
        assert vertical_rule not in registered
        with pytest.raises(KeyError):
            execute_rule(
                vertical_rule, {}, InferenceContext(tenant_id="t", domain_id="orbital-logistics", pass_number=1)
            )


# ── Outcome vocabulary is the domain's ──────────────────────────────────────


@pytest.mark.asyncio
async def test_outcome_node_label_comes_from_the_domain_spec() -> None:
    from engine.handlers import handle_outcomes
    from engine.state import _reset_singleton

    raw = synthetic_loader().load_domain("orbital-logistics").model_dump(by_alias=True)
    raw["feedbackloop"] = {"enabled": False, "outcome_node_label": "DockingResult"}
    spec = DomainSpec.model_validate(raw)
    _driver, calls = _bind_handlers(spec)
    try:
        result = await handle_outcomes(
            "orbital-logistics",
            {"match_id": "m1", "candidate_id": "depot-1", "outcome": "success"},
        )
    finally:
        _reset_singleton()

    assert result["status"] == "recorded"
    writes = [cypher for cypher, _ in calls if "CREATE (o:" in cypher]
    assert writes, calls
    assert "CREATE (o:DockingResult {" in writes[0]
    assert "TransactionOutcome" not in writes[0]


def test_unregistered_inference_rule_name_withholds_the_pack(tmp_path: Path) -> None:
    """plugins.inference_rules names registered rules; a name the registry lacks is an admission defect."""
    from engine.config.loader import admission_defects
    from engine.inference_rule_registry import list_registered_rules

    raw = (SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text()
    (tmp_path / "cheese-aging").mkdir()
    (tmp_path / "cheese-aging" / "spec.yaml").write_text(
        raw + "\nplugins:\n  inference_rules:\n    - infer_company_size_tier\n    - infer_wheel_ripeness\n"
    )
    with pytest.raises(DomainNotFoundError, match="'infer_wheel_ripeness' is not a registered inference rule"):
        DomainPackLoader(config_path=str(tmp_path)).load_domain("cheese-aging")

    assert "infer_company_size_tier" in list_registered_rules()
    good = DomainSpec.model_validate(
        {**yaml.safe_load(raw), "plugins": {"inference_rules": ["infer_company_size_tier"]}}
    )
    assert admission_defects(good) == []
