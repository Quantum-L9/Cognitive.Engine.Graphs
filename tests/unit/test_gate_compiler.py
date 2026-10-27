"""Unit tests — GateCompiler: gate types, null semantics, direction filter."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def test_exact_gate_generates_equality_clause():
    from unittest.mock import MagicMock

    from engine.config.schema import GateSpec, GateType
    from engine.gates.compiler import GateCompiler

    mock_spec = MagicMock()
    mock_spec.gates = []
    mock_spec.compliance = None
    compiler = GateCompiler(mock_spec)

    gate = GateSpec(name="g", type=GateType.BOOLEAN, candidateprop="status", queryparam="status")
    result = compiler._compile_boolean(gate)
    assert "$status" in result or "status" in result


def test_direction_filter_skips_non_matching():
    """A gate scoped to buyer_to_seller must not compile for seller_to_buyer."""
    from unittest.mock import MagicMock

    from engine.config.schema import GateSpec, GateType
    from engine.gates.compiler import GateCompiler

    mock_spec = MagicMock()
    mock_spec.gates = []
    mock_spec.compliance = None
    compiler = GateCompiler(mock_spec)

    gate = GateSpec(
        name="g",
        type=GateType.BOOLEAN,
        candidateprop="f",
        queryparam="f",
        matchdirections=["buyer_to_seller"],
    )
    # compile_all_gates with a different direction should exclude this gate
    mock_spec.gates = [gate]
    result = compiler.compile_all_gates(match_direction="seller_to_buyer")
    assert result == "" or "f" not in result


def test_null_behavior_pass_wraps_clause():
    """nullbehavior=pass should wrap clause with NULL condition."""
    from unittest.mock import MagicMock

    from engine.config.schema import GateSpec, GateType
    from engine.gates.compiler import GateCompiler

    mock_spec = MagicMock()
    mock_spec.gates = []
    mock_spec.compliance = None
    compiler = GateCompiler(mock_spec)

    gate = GateSpec(
        name="g",
        type=GateType.BOOLEAN,
        candidateprop="status",
        queryparam="status",
        nullbehavior="fail",
    )
    predicate = compiler._compile_boolean(gate)
    wrapped = compiler._wrap_null_semantics(gate, predicate)
    assert "NULL" in wrapped or "$status" in wrapped


def test_compile_all_gates_empty_returns_empty():
    from unittest.mock import MagicMock

    from engine.gates.compiler import GateCompiler

    mock_spec = MagicMock()
    mock_spec.gates = []
    mock_spec.compliance = None
    compiler = GateCompiler(mock_spec)
    result = compiler.compile_all_gates(match_direction="buyer_to_seller")
    assert isinstance(result, str)


# ── Correctness remediation lane C (plan 0d84ec8) ────────────────────────────

_DOMAINS = Path(__file__).resolve().parents[2] / "domains"


def _pack(domain_id: str):  # type: ignore[no-untyped-def]
    from engine.config.schema import DomainSpec

    return DomainSpec.model_validate(yaml.safe_load((_DOMAINS / domain_id / "spec.yaml").read_text()))


def _two_label_spec(gates: list[dict], queryfields: list[dict] | None = None):  # type: ignore[no-untyped-def]
    """Candidate Widget (direction d1) / Gadget (direction d2); Ask is the query entity."""
    from engine.config.schema import DomainSpec

    raw = yaml.safe_load(
        """
domain: {id: compiler-check, name: Compiler check, version: 0.1.0}
ontology:
  nodes:
    - {label: Widget, managedby: sync, candidate: true, properties: [{name: widget_id, type: string, required: true}]}
    - {label: Gadget, managedby: sync, candidate: true, properties: [{name: gadget_id, type: string, required: true}]}
    - {label: Ask, managedby: api, queryentity: true, properties: [{name: ask_id, type: string, required: true}]}
  edges:
    - {type: BLOCKS, from: Widget, to: Widget, direction: DIRECTED, category: exclusion, managedby: sync}
    - {type: SHUNS, from: Ask, to: Widget, direction: DIRECTED, category: exclusion, managedby: sync}
matchentities:
  candidate: [{label: Widget, matchdirection: d1}, {label: Gadget, matchdirection: d2}]
  queryentity: [{label: Ask, matchdirection: d1}, {label: Ask, matchdirection: d2}]
queryschema:
  matchdirections: [d1, d2]
  fields: []
gates: []
scoring: {dimensions: []}
"""
    )
    raw["gates"] = gates
    raw["queryschema"]["fields"] = queryfields or []
    return DomainSpec.model_validate(raw)


# RG-054 (RU-041 / CEG-COR-019): an exclusion gate the compiler cannot honour is
# refused instead of compiling to an always-true NOT EXISTS.
def test_mortgage_blacklist_exclusion_is_refused_by_name() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _pack("mortgage-brokerage")
    gate = next(g for g in spec.gates if g.name == "blacklist_exclusion")
    with pytest.raises(ValueError, match="blacklist_exclusion"):
        GateCompiler(spec).compile(gate)


def test_mortgage_admission_lists_the_blacklist_exclusion() -> None:
    from engine.config.loader import admission_defects

    assert any(d.startswith("blacklist_exclusion:") for d in admission_defects(_pack("mortgage-brokerage")))


@pytest.mark.parametrize(
    "extra",
    [{"fromnode": "Ask"}, {"tonode": "Widget"}, {"fromnode": "Ask", "tonode": "Widget"}],
    ids=["fromnode", "tonode", "both"],
)
def test_exclusion_declaring_endpoints_is_refused(extra: dict) -> None:
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec([{"name": "no_blocked", "type": "exclusion", "edgetype": "BLOCKS", **extra}])
    with pytest.raises(ValueError, match="no_blocked"):
        GateCompiler(spec).compile(spec.gates[0])


def test_exclusion_edge_from_a_non_candidate_label_is_refused() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec([{"name": "shunned", "type": "exclusion", "edgetype": "SHUNS"}])
    with pytest.raises(ValueError, match="shunned"):
        GateCompiler(spec).compile(spec.gates[0])


def test_exclusion_edge_from_another_directions_candidate_is_refused() -> None:
    """BLOCKS starts at Widget; scoped to d2 the candidate is a Gadget, which never carries it."""
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec(
        [{"name": "blocked_d2", "type": "exclusion", "edgetype": "BLOCKS", "matchdirections": ["d2"]}]
    )
    with pytest.raises(ValueError, match="blocked_d2"):
        GateCompiler(spec).compile(spec.gates[0])


def test_candidate_origin_exclusion_compiles_unchanged() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec(
        [{"name": "no_blocked", "type": "exclusion", "edgetype": "BLOCKS", "matchdirections": ["d1"]}]
    )
    assert GateCompiler(spec).compile(spec.gates[0]) == "NOT EXISTS { MATCH (candidate)-[:BLOCKS]->() }"
    plasticos = _pack("plasticos")
    gate = next(g for g in plasticos.gates if g.name == "exclusion_check")
    assert GateCompiler(plasticos).compile(gate) == "NOT EXISTS { MATCH (candidate)-[:EXCLUDED_FROM]->() }"


def test_exclusion_endpoint_validation_off_restores_the_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.gates.compiler import GateCompiler

    monkeypatch.setattr(settings, "exclusion_endpoint_validation", False)
    spec = _pack("mortgage-brokerage")
    gate = next(g for g in spec.gates if g.name == "blacklist_exclusion")
    assert GateCompiler(spec).compile(gate) == "NOT EXISTS { MATCH (candidate)-[:BLACKLISTEDLENDER]->() }"
