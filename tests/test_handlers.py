"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, handlers]
owner: engine-team
status: active
--- /L9_META ---

Tests for engine/handlers.py - all 6 action handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.handlers import (
    ValidationError,
    handle_admin,
    handle_health,
    handle_match,
    handle_outcomes,
    handle_resolve,
    handle_sync,
    init_dependencies,
)

# ── Stage 2 P4: candidate identity comes from the domain, not a hardcoded entity_id ──


def _spec_with_sync(label: str, idproperty: str | None, required_props: list[str]) -> MagicMock:
    spec = MagicMock()
    node = MagicMock()
    node.label = label
    node.properties = []
    for name in required_props:
        prop = MagicMock()
        prop.name = name
        prop.required = True
        node.properties.append(prop)
    spec.ontology.nodes = [node]
    if idproperty is None:
        spec.sync = None
    else:
        endpoint = MagicMock()
        endpoint.targetnode = label
        endpoint.idproperty = idproperty
        spec.sync.endpoints = [endpoint]
    return spec


def test_candidate_id_property_prefers_sync_idproperty() -> None:
    from engine.handlers import _candidate_id_property

    spec = _spec_with_sync("Depot", "depot_code", ["depot_code", "depot_name"])
    assert _candidate_id_property(spec, "Depot") == "depot_code"


def test_candidate_id_property_falls_back_to_first_required_property() -> None:
    from engine.handlers import _candidate_id_property

    spec = _spec_with_sync("Laboratory", None, ["lab_id", "lab_name"])
    assert _candidate_id_property(spec, "Laboratory") == "lab_id"


def test_candidate_id_property_falls_back_to_entity_id() -> None:
    from engine.handlers import _candidate_id_property

    spec = _spec_with_sync("Thing", None, [])
    assert _candidate_id_property(spec, "Thing") == "entity_id"


def test_candidate_id_property_legacy_behind_flag(monkeypatch) -> None:
    from engine.config.settings import settings
    from engine.handlers import _candidate_id_property

    monkeypatch.setattr(settings, "spec_declared_candidate_id", False)
    spec = _spec_with_sync("Depot", "depot_code", ["depot_code"])
    assert _candidate_id_property(spec, "Depot") == "entity_id"


def test_candidate_id_reads_declared_then_entity_id_then_fallback() -> None:
    from engine.handlers import _candidate_id

    assert _candidate_id({"depot_code": "D-1"}, "depot_code", 7) == "D-1"
    assert _candidate_id({"entity_id": "e-9"}, "depot_code", 7) == "e-9"  # legacy nodes still resolve
    assert _candidate_id({}, "depot_code", 7) == "7"
    assert _candidate_id("not-a-node", "depot_code", 3) == "3"


def _make_domain_spec() -> MagicMock:
    spec = MagicMock()
    spec.domain.id = "test_domain"
    spec.domain.name = "Test"
    spec.domain.version = "1.0"

    gate = MagicMock()
    gate.matchdirections = None
    gate.candidateprop = "grade"
    gate.queryparam = "min_grade"
    gate.name = "grade_gate"
    spec.gates = [gate]

    candidate = MagicMock()
    candidate.label = "Facility"
    candidate.matchdirection = "buyer_to_seller"
    spec.matchentities.candidate = [candidate]

    step = MagicMock()
    step.matchdirections = None
    step.required = True
    step.pattern = "(candidate)-[:ACCEPTS]->(mat)"
    step.name = "taxonomy"
    spec.traversal.steps = [step]

    dim = MagicMock()
    dim.matchdirections = None
    dim.name = "structural"
    dim.weightkey = "structural"
    dim.defaultweight = 0.4
    spec.scoring.dimensions = [dim]

    spec.derivedparameters = []
    spec.compliance = None
    # RU-010 (R1a-DEC-03): the gate reads $min_grade, so the query schema declares
    # it (optional); an undeclared gate parameter is refused before the query.
    grade_field = MagicMock(required=False, default=None)
    grade_field.name = "min_grade"
    spec.queryschema.fields = [grade_field]

    # A node endpoint with no graph-ref namespace, as SyncEndpointSpec defaults
    # it (attributes set in the constructor keep this fixture under the
    # statement limit).
    endpoint = MagicMock(targetedge=None, refnamespace=None)
    endpoint.path = "/v1/sync/facility"
    endpoint.batchstrategy = "UNWINDMERGE"
    endpoint.targetnode = "Facility"
    endpoint.idproperty = "facility_id"
    endpoint.taxonomyedges = []
    endpoint.childsync = []
    spec.sync.endpoints = [endpoint]

    spec.gdsjobs = []

    node = MagicMock()
    node.label = "Facility"
    prop = MagicMock()
    prop.name = "facility_id"
    prop.required = True
    node.properties = [prop]
    spec.ontology.nodes = [node]

    # Feedback loop: unset MagicMock attribute chains would leak into
    # sanitize_label() (which requires str), so pin real values and keep
    # the loop disabled — handlers then skip score propagation entirely.
    spec.feedbackloop.enabled = False
    spec.feedbackloop.signal_weights.enabled = False
    spec.feedbackloop.outcome_node_label = "Outcome"

    return spec


@pytest.fixture(autouse=True)
def _inject_deps() -> None:
    mock_driver = AsyncMock()
    mock_driver.execute_query = AsyncMock(return_value=[])
    mock_loader = MagicMock()
    mock_loader.load_domain = MagicMock(return_value=_make_domain_spec())
    mock_loader.list_domains = MagicMock(return_value=["plasticos", "mortgage"])
    init_dependencies(mock_driver, mock_loader)


@pytest.mark.asyncio
async def test_match_returns_structure() -> None:
    result = await handle_match(
        "t",
        {
            "query": {"polymer": "HDPE"},
            "match_direction": "buyer_to_seller",
        },
    )
    assert "candidates" in result
    assert "query_id" in result
    assert isinstance(result["execution_time_ms"], float)
    assert result["status"] == "success"  # RU-014 / CEG-COR-075


@pytest.mark.asyncio
async def test_match_missing_query() -> None:
    with pytest.raises(ValidationError, match="query"):
        await handle_match("t", {"match_direction": "buyer_to_seller"})


@pytest.mark.asyncio
async def test_match_missing_direction() -> None:
    with pytest.raises(ValidationError, match="match_direction"):
        await handle_match("t", {"query": {}})


@pytest.mark.asyncio
async def test_match_bad_direction() -> None:
    with pytest.raises(ValidationError, match="No candidate entity"):
        await handle_match("t", {"query": {}, "match_direction": "nonexistent"})


@pytest.mark.asyncio
async def test_sync_success() -> None:
    result = await handle_sync(
        "t",
        {
            "entity_type": "facility",
            "batch": [{"facility_id": "F1"}],
        },
    )
    assert result["status"] == "success"
    assert result["synced_count"] == 1


@pytest.mark.asyncio
async def test_sync_empty_batch() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        await handle_sync("t", {"entity_type": "facility", "batch": []})


@pytest.mark.asyncio
async def test_sync_unknown_entity() -> None:
    with pytest.raises(ValidationError, match="No sync endpoint"):
        await handle_sync("t", {"entity_type": "bogus", "batch": [{"id": 1}]})


@pytest.mark.asyncio
async def test_admin_list_domains() -> None:
    result = await handle_admin("t", {"subaction": "list_domains"})
    assert "plasticos" in result["domains"]


@pytest.mark.asyncio
async def test_admin_get_domain() -> None:
    result = await handle_admin("plasticos", {"subaction": "get_domain", "domain_id": "plasticos"})
    assert "domain" in result


@pytest.mark.asyncio
async def test_admin_init_schema() -> None:
    result = await handle_admin("plasticos", {"subaction": "init_schema", "domain_id": "plasticos"})
    assert result["status"] == "schema_initialized"


@pytest.mark.asyncio
async def test_admin_unknown_subaction() -> None:
    with pytest.raises(ValidationError, match="Unknown admin subaction"):
        await handle_admin("t", {"subaction": "drop_everything"})


@pytest.mark.asyncio
async def test_outcomes_success() -> None:
    result = await handle_outcomes(
        "t",
        {
            "match_id": "q_abc",
            "candidate_id": "F42",
            "outcome": "success",
        },
    )
    assert result["status"] == "recorded"
    assert result["outcome_id"].startswith("out_")


@pytest.mark.asyncio
async def test_outcomes_invalid_outcome() -> None:
    with pytest.raises(ValidationError, match="Invalid outcome"):
        await handle_outcomes(
            "t",
            {
                "match_id": "q_abc",
                "candidate_id": "F42",
                "outcome": "maybe",
            },
        )


@pytest.mark.asyncio
async def test_resolve_success() -> None:
    from engine.state import get_state

    # RU-004: success means the CREATE returned the RESOLVED_FROM row.
    get_state().graph_driver.execute_query.return_value = [{"resolution_id": "res_0123456789ab"}]
    result = await handle_resolve(
        "t",
        {
            "entity_type": "MaterialProfile",
            "source_id": "MP1",
            "target_id": "MP2",
            "confidence": 0.95,
            "signal": "llm_consensus",
        },
    )
    assert result["status"] == "resolved"
    assert result["resolution_id"].startswith("res_")


@pytest.mark.asyncio
async def test_resolve_missing_field() -> None:
    with pytest.raises(ValidationError, match="source_id"):
        await handle_resolve("t", {"entity_type": "MP", "target_id": "MP2"})


@pytest.mark.asyncio
async def test_health_ok() -> None:
    result = await handle_health("t", {})
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_degraded() -> None:
    from engine.state import get_state

    state = get_state()
    state.graph_driver.execute_query = AsyncMock(side_effect=ConnectionError("refused"))
    result = await handle_health("t", {})
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_neo4j_ok_domain_missing_tenant_but_packs_available() -> None:
    """Health is degraded (not errored) when tenant spec is absent but other packs exist."""
    from engine.config.loader import DomainNotFoundError
    from engine.state import get_state

    state = get_state()
    state.graph_driver.execute_query = AsyncMock(return_value=None)
    state.domain_loader.load_domain = MagicMock(side_effect=DomainNotFoundError("not found"))
    state.domain_loader.list_domains = MagicMock(return_value=["plasticos"])

    result = await handle_health("unknown-tenant", {})

    assert result["status"] == "degraded"
    assert "neo4j" in result["checks"]
    assert result["checks"]["neo4j"] == "ok"
    assert "tenant not found" in result["checks"]["domain_spec"]
    # RU-013 / CEG-COR-074: the unauthenticated probe reports a count, never the
    # other tenants' domain ids (this assertion used to require the leak).
    assert "plasticos" not in result["checks"]["domain_spec"]
    assert result["checks"]["domain_spec"] == "ok: tenant not found; fallback available: 1 domain packs"


@pytest.mark.asyncio
async def test_health_domain_ids_listed_with_count_only_flag_off(monkeypatch) -> None:
    """HEALTH_PROBE_DOMAIN_COUNT_ONLY=false restores the id list (rollback)."""
    from engine.config.loader import DomainNotFoundError
    from engine.config.settings import settings
    from engine.state import get_state

    monkeypatch.setattr(settings, "health_probe_domain_count_only", False)
    state = get_state()
    state.graph_driver.execute_query = AsyncMock(return_value=None)
    state.domain_loader.load_domain = MagicMock(side_effect=DomainNotFoundError("not found"))
    state.domain_loader.list_domains = MagicMock(return_value=["plasticos"])

    result = await handle_health("unknown-tenant", {})

    assert result["checks"]["domain_spec"] == "ok: tenant not found; fallback available: ['plasticos']"


@pytest.mark.asyncio
async def test_health_neo4j_ok_no_domains_at_all() -> None:
    """Health is degraded when no domain packs are configured."""
    from engine.config.loader import DomainNotFoundError
    from engine.state import get_state

    state = get_state()
    state.graph_driver.execute_query = AsyncMock(return_value=None)
    state.domain_loader.load_domain = MagicMock(side_effect=DomainNotFoundError("not found"))
    state.domain_loader.list_domains = MagicMock(return_value=[])

    result = await handle_health("plasticos", {})

    assert result["status"] == "degraded"
    assert result["checks"]["domain_spec"] == "error: no_domains_found"


@pytest.mark.asyncio
async def test_health_neo4j_connection_failed() -> None:
    """Neo4j unreachable reports degraded with connection_failed check."""
    from engine.state import get_state

    state = get_state()
    state.graph_driver.execute_query = AsyncMock(side_effect=OSError("refused"))

    result = await handle_health("plasticos", {})

    assert result["status"] == "degraded"
    assert result["checks"]["neo4j"] == "error: connection_failed"


def _loaded_spec_for_results() -> MagicMock:
    """The autouse spec, with the MagicMock-only branches handle_match would trip on pinned off."""
    from engine.state import get_state

    spec = get_state().domain_loader.load_domain.return_value
    spec.decision_arbitration.enabled = False
    spec.causal.enabled = False
    return spec


@pytest.mark.asyncio
async def test_match_null_scores_are_coerced_before_normalization(monkeypatch) -> None:
    """A null `score` (clamp off, unscored property) is stored as 0.0, so normalization
    neither subtracts from None nor promotes a lone null to 1.0."""
    from engine.config.settings import settings
    from engine.state import get_state

    _loaded_spec_for_results()
    monkeypatch.setattr(settings, "score_normalize", True)
    get_state().graph_driver.execute_query = AsyncMock(
        return_value=[
            {"candidate": {"facility_id": "F-1"}, "score": None, "structural": 0.2},
            {"candidate": {"facility_id": "F-2"}, "score": 0.4, "structural": 0.8},
        ]
    )

    result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})

    scores = [c["score"] for c in result["candidates"]]
    assert scores == [0.0, 1.0]
    assert result["scoring_meta"]["raw_min"] == 0.0
    assert result["scoring_meta"]["raw_max"] == 0.4
    assert result["scoring_meta"]["normalization_applied"] is True


@pytest.mark.asyncio
async def test_match_lone_null_score_normalizes_to_zero(monkeypatch) -> None:
    from engine.config.settings import settings
    from engine.state import get_state

    _loaded_spec_for_results()
    monkeypatch.setattr(settings, "score_normalize", True)
    get_state().graph_driver.execute_query = AsyncMock(
        return_value=[{"candidate": {"facility_id": "F-1"}, "score": None, "structural": 0.2}]
    )

    result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})

    # Range is zero, so the singleton branch runs — on a real 0.0, not on None.
    assert result["candidates"][0]["score"] == 1.0
    assert result["scoring_meta"]["raw_max"] == 0.0


@pytest.mark.asyncio
async def test_match_arbitration_front_uses_declared_candidate_id() -> None:
    """`pareto.arbitration.nondominated_ids` carries the same declared ids as `pareto.non_dominated_ids`."""
    from engine.state import get_state

    spec = _loaded_spec_for_results()
    spec.decision_arbitration.enabled = True
    spec.decision_arbitration.constraints = []
    objective = MagicMock()
    objective.dimension = "structural"
    spec.decision_arbitration.pareto_config.objectives = [objective]
    get_state().graph_driver.execute_query = AsyncMock(
        return_value=[
            {"candidate": {"facility_id": "F-1"}, "score": 0.9, "structural": 0.9},
            {"candidate": {"facility_id": "F-2"}, "score": 0.5, "structural": 0.5},
        ]
    )

    result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})

    pareto = result["pareto"]
    assert pareto["arbitration"]["nondominated_ids"] == ["F-1"]
    assert pareto["non_dominated_ids"] == ["F-1"]


@pytest.mark.asyncio
async def test_match_arbitration_forwards_direction_and_front_size_limit() -> None:
    """RU-019 / CEG-COR-026: handle_match passes the declared objective direction and front_size_limit."""
    from engine.state import get_state

    spec = _loaded_spec_for_results()
    spec.decision_arbitration.enabled = True
    spec.decision_arbitration.constraints = []
    objective = MagicMock()
    objective.dimension = "cost"
    objective.direction = "minimize"
    spec.decision_arbitration.pareto_config.objectives = [objective]
    spec.decision_arbitration.pareto_config.front_size_limit = 1
    get_state().graph_driver.execute_query = AsyncMock(
        return_value=[
            {"candidate": {"facility_id": "F-1"}, "score": 0.4, "cost": 0.1},
            {"candidate": {"facility_id": "F-2"}, "score": 0.6, "cost": 0.9},
            {"candidate": {"facility_id": "F-3"}, "score": 0.9, "cost": 0.1},
        ]
    )

    result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})

    # cost is minimised: F-1 and F-3 tie on the front; the limit keeps the higher score.
    assert result["pareto"]["arbitration"]["nondominated_ids"] == ["F-3"]
    assert result["pareto"]["arbitration"]["frontsize"] == 1


@pytest.mark.asyncio
async def test_match_binds_compiler_literals_into_query_parameters() -> None:
    """CEG-009b: a literal-operand gate reaches execute_query bound.

    `GateCompiler` binds `queryparam: 85.0` under `$gate_<name>_value`;
    handle_match must merge `gate_compiler.last_query_params` into the
    parameters or Neo4j answers ParameterMissing for every such gate.
    """
    from engine.config.schema import GateType, NullBehavior
    from engine.state import get_state

    spec = _make_domain_spec()
    gate = spec.gates[0]
    gate.type = GateType.THRESHOLD
    gate.candidateprop = "utilization"
    gate.queryparam = 85.0
    gate.operator = "<="
    gate.nullbehavior = NullBehavior.PASS
    gate.roleexempt = None
    get_state().domain_loader.load_domain = MagicMock(return_value=spec)
    driver = get_state().graph_driver

    await handle_match("t", {"query": {"urgency": "routine"}, "match_direction": "buyer_to_seller"})

    call = driver.execute_query.call_args
    cypher = call.kwargs["cypher"]
    parameters = call.kwargs["parameters"]
    assert "candidate.utilization <= $gate_grade_gate_value" in cypher
    assert "$85" not in cypher
    assert parameters["gate_grade_gate_value"] == 85.0
    assert parameters["urgency"] == "routine"
    assert "top_n" in parameters


# ── RU-061 / CEG-COR-008: prohibited factors block before any Cypher runs ──


def _mortgage_spec_with(*, dimension_prop: str | None = None, gate_update: dict | None = None):
    import yaml

    from engine.config.schema import DomainSpec

    spec = DomainSpec.model_validate(
        yaml.safe_load(Path("domains/mortgage-brokerage/spec.yaml").read_text(encoding="utf-8"))
    )
    if dimension_prop is not None:
        dims = list(spec.scoring.dimensions)
        dims[0] = dims[0].model_copy(update={"candidateprop": dimension_prop})
        spec = spec.model_copy(update={"scoring": spec.scoring.model_copy(update={"dimensions": dims})})
    if gate_update is not None:
        gates = list(spec.gates)
        gates[0] = gates[0].model_copy(update=gate_update)
        spec = spec.model_copy(update={"gates": gates})
    return spec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec_kwargs",
    [
        {"dimension_prop": "age"},
        {"gate_update": {"candidateprop": "Race"}},
        {"gate_update": {"candidateprop_min": "age"}},
    ],
)
async def test_match_blocks_prohibited_reference_before_any_query(spec_kwargs: dict) -> None:
    from engine.state import get_state

    spec = _mortgage_spec_with(**spec_kwargs)
    state = get_state()
    state.compliance_engines.pop(spec.domain.id, None)
    state.domain_loader.load_domain = MagicMock(return_value=spec)
    driver = state.graph_driver

    with pytest.raises(ValueError, match="prohibited field"):
        await handle_match("t", {"query": {"creditscore": 700}, "match_direction": "borrowertoproduct"})
    driver.execute_query.assert_not_awaited()


# ── RU-062 / CEG-COR-009: candidate PII is redacted on the match response ──


@pytest.mark.asyncio
async def test_match_response_candidate_lacks_raw_pii() -> None:
    from engine.config.schema import ComplianceSpec, PIISpec
    from engine.state import get_state

    spec = _loaded_spec_for_results()
    spec.compliance = ComplianceSpec(pii=PIISpec(enabled=True))  # the domain opts in to PII handling
    get_state().compliance_engines.pop("test_domain", None)
    get_state().graph_driver.execute_query = AsyncMock(
        return_value=[{"candidate": {"facility_id": "F-1", "contact_email": "ops@f1.com"}, "score": 0.9}]
    )

    result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})

    assert "ops@f1.com" not in str(result["candidates"])
    assert result["candidates"][0]["candidate"]["facility_id"] == "F-1"


# ── RU-065 / CEG-COR-053: an audit sink failure never fails a request ──


class _FailingPool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self):
                msg = "postgres unavailable"
                raise ConnectionError(msg)

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


@pytest.mark.asyncio
async def test_match_and_sync_succeed_when_audit_sink_fails(caplog: pytest.LogCaptureFixture) -> None:
    from engine.state import get_state

    state = get_state()
    init_dependencies(state.graph_driver, state.domain_loader, db_pool=_FailingPool())
    _loaded_spec_for_results()
    state.compliance_engines.pop("test_domain", None)
    state.graph_driver.execute_query = AsyncMock(return_value=[{"candidate": {"facility_id": "F-1"}, "score": 0.9}])
    try:
        with caplog.at_level("WARNING"):
            match = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})
            sync = await handle_sync("t", {"entity_type": "facility", "batch": [{"facility_id": "F1"}]})
    finally:
        state.compliance_engines.pop("test_domain", None)
        init_dependencies(state.graph_driver, state.domain_loader)
    assert match["candidates"][0]["candidate"]["facility_id"] == "F-1"
    assert sync["status"] == "success"
    assert any("audit flush failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_match_violation_leaves_denied_entry_in_domain_engine() -> None:
    """RU-063: after the request fails, EngineState's domain ComplianceEngine holds the denied entry."""
    from engine.state import get_state

    spec = _mortgage_spec_with(dimension_prop="age")
    state = get_state()
    state.compliance_engines.pop(spec.domain.id, None)
    state.domain_loader.load_domain = MagicMock(return_value=spec)

    with pytest.raises(ValueError, match="prohibited field"):
        await handle_match("acme", {"query": {"creditscore": 700}, "match_direction": "borrowertoproduct"})
    entries = state.compliance_engines[spec.domain.id]._audit.flush_sync()
    denied = [e for e in entries if e.outcome == "denied"]
    assert len(denied) == 1
    assert denied[0].actor == "acme"
    state.compliance_engines.pop(spec.domain.id, None)


@pytest.mark.asyncio
async def test_declared_pii_id_property_does_not_change_pareto_ids() -> None:
    """RU-064: Pareto ids are computed before redact_response, so hashing a declared
    PII field that is also the candidate id property leaves them unaffected."""
    from engine.compliance.pii import PIIHandler
    from engine.config.schema import ComplianceSpec, PIISpec
    from engine.state import get_state

    spec = _loaded_spec_for_results()
    spec.compliance = ComplianceSpec(pii=PIISpec(enabled=True, fields=["facility_id"], handling="hash"))
    spec.decision_arbitration.enabled = True
    spec.decision_arbitration.constraints = []
    objective = MagicMock()
    objective.dimension = "structural"
    spec.decision_arbitration.pareto_config.objectives = [objective]
    state = get_state()
    state.compliance_engines.pop("test_domain", None)
    state.graph_driver.execute_query = AsyncMock(
        return_value=[
            {"candidate": {"facility_id": "F-1"}, "score": 0.9, "structural": 0.9},
            {"candidate": {"facility_id": "F-2"}, "score": 0.5, "structural": 0.5},
        ]
    )
    try:
        result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})
    finally:
        state.compliance_engines.pop("test_domain", None)

    assert result["pareto"]["non_dominated_ids"] == ["F-1"]
    assert result["candidates"][0]["candidate"]["facility_id"] == PIIHandler.hash_value("F-1")


@pytest.mark.asyncio
async def test_sync_rejects_inverted_causal_records_by_batch_edge_type() -> None:
    """RU-069 / CEG-COR-058: sync rows carry no `edge_type`; the batch's edge type decides."""
    from engine.config.schema import CausalEdgeSpec, CausalSubgraphSpec
    from engine.state import get_state

    spec = get_state().domain_loader.load_domain.return_value
    spec.causal = CausalSubgraphSpec(
        enabled=True,
        causal_edges=[CausalEdgeSpec(edge_type="RESULTED_IN", source_label="Facility", target_label="Outcome")],
    )
    result = await handle_sync(
        "t",
        {
            "entity_type": "facility",
            "edge_type": "RESULTED_IN",
            "batch": [
                {"facility_id": "F1", "source_ts": "2026-03-01T00:00:00", "target_ts": "2026-03-02T00:00:00"},
                {"facility_id": "F2", "source_ts": "2026-03-02T00:00:00", "target_ts": "2026-03-01T00:00:00"},
            ],
        },
    )
    assert result["status"] == "success"
    assert result["synced_count"] == 1
    assert result["rejected_count"] == 1


@pytest.mark.asyncio
async def test_match_response_keeps_candidate_fields_when_domain_opts_out_of_pii() -> None:
    from engine.config.schema import ComplianceSpec, PIISpec
    from engine.state import get_state

    spec = _loaded_spec_for_results()
    spec.compliance = ComplianceSpec(pii=PIISpec(enabled=False))  # idea-portfolio: compliance.pii: {enabled: false}
    get_state().compliance_engines.pop("test_domain", None)
    get_state().graph_driver.execute_query = AsyncMock(
        return_value=[{"candidate": {"facility_id": "F-1", "contact_email": "ops@f1.com"}, "score": 0.9}]
    )

    result = await handle_match("t", {"query": {"polymer": "HDPE"}, "match_direction": "buyer_to_seller"})

    assert result["candidates"][0]["candidate"]["contact_email"] == "ops@f1.com"
    assert result["candidates"][0]["candidate"]["facility_id"] == "F-1"
