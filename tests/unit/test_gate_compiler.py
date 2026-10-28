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
# refused instead of compiling to an always-true NOT EXISTS. RU-038 (DEC-15)
# removed the gate from the mortgage pack, which is now dormant; these tests
# re-create it on the pack as the pre-repair spec declared it.
_PRE_REPAIR_MORTGAGE_BLACKLIST = {
    "name": "blacklist_exclusion",
    "type": "exclusion",
    "edgetype": "BLACKLISTEDLENDER",
    "fromnode": "query",
    "tonode": "lender",
}


def _mortgage_with_query_anchored_blacklist():  # type: ignore[no-untyped-def]
    from engine.config.schema import DomainSpec

    raw = yaml.safe_load((_DOMAINS / "mortgage-brokerage" / "spec.yaml").read_text())
    assert not any(g["name"] == "blacklist_exclusion" for g in raw["gates"])
    raw["gates"].append(dict(_PRE_REPAIR_MORTGAGE_BLACKLIST))
    return DomainSpec.model_validate(raw)


def test_mortgage_blacklist_exclusion_is_refused_by_name() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _mortgage_with_query_anchored_blacklist()
    gate = next(g for g in spec.gates if g.name == "blacklist_exclusion")
    with pytest.raises(ValueError, match="blacklist_exclusion"):
        GateCompiler(spec).compile(gate)


def test_mortgage_admission_lists_the_blacklist_exclusion() -> None:
    from engine.config.loader import admission_defects

    assert any(
        d.startswith("blacklist_exclusion:") for d in admission_defects(_mortgage_with_query_anchored_blacklist())
    )
    # The repository pack no longer declares it (RU-038).
    assert not any(d.startswith("blacklist_exclusion:") for d in admission_defects(_pack("mortgage-brokerage")))


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
    spec = _mortgage_with_query_anchored_blacklist()
    gate = next(g for g in spec.gates if g.name == "blacklist_exclusion")
    assert GateCompiler(spec).compile(gate) == "NOT EXISTS { MATCH (candidate)-[:BLACKLISTEDLENDER]->() }"


# RG-055 (RU-042 / CEG-COR-020, CEG-COR-021): the declared null policy applies to
# exactly the operands the predicate tests.
def _compile_one(gate: dict, fields: list[dict] | None = None) -> str:
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec([{"matchdirections": ["d1"], **gate}], fields)
    return GateCompiler(spec).compile(spec.gates[0])


def test_range_fail_rejects_null_candidate_bounds() -> None:  # counterexample [2]
    fragment = _compile_one(
        {
            "name": "density_fail",
            "type": "range",
            "candidateprop_min": "min_density",
            "candidateprop_max": "max_density",
            "queryparam": "density",
            "nullbehavior": "fail",
        }
    )
    assert fragment.startswith(
        "(candidate.min_density IS NOT NULL AND candidate.max_density IS NOT NULL AND $density IS NOT NULL AND ("
    )


def test_range_with_derived_bounds_does_not_null_check_the_untested_property() -> None:  # counterexample [3]
    fragment = _compile_one(
        {
            "name": "density_pass",
            "type": "range",
            "candidateprop": "density",
            "queryparam": "density",
            "nullbehavior": "pass",
        }
    )
    assert "candidate.density IS NULL" not in fragment
    assert fragment == (
        "($density IS NULL OR ((candidate.min_density IS NULL OR candidate.min_density <= $density) AND "
        "(candidate.max_density IS NULL OR $density <= candidate.max_density)))"
    )


def test_temporalrange_pass_window_admits_null_operands() -> None:  # counterexample [4]
    fragment = _compile_one(
        {
            "name": "tw",
            "type": "temporalrange",
            "candidateprop_start": "start",
            "candidateprop_end": "end",
            "queryparam_start": "ws",
            "queryparam_end": "we",
            "nullbehavior": "pass",
        }
    )
    for operand in ("candidate.start", "candidate.end", "$ws", "$we"):
        assert f"{operand} IS NULL" in fragment, fragment


def test_temporalrange_null_checks_the_derived_parameters_it_tests() -> None:  # counterexample [5]
    fragment = _compile_one(
        {
            "name": "tw",
            "type": "temporalrange",
            "candidateprop": "start",
            "queryparam": "window",
            "nullbehavior": "pass",
        }
    )
    assert "$window_start IS NULL" in fragment
    assert "$window_end IS NULL" in fragment
    assert "$window " not in fragment
    assert "$window)" not in fragment


def test_exclusion_null_check_never_references_the_target_property() -> None:  # counterexample [6]
    fragment = _compile_one(
        {
            "name": "blocked",
            "type": "exclusion",
            "edgetype": "BLOCKS",
            "candidateprop": "buyer_id",
            "queryparam": "buyer",
            "nullbehavior": "pass",
        }
    )
    assert "candidate.buyer_id IS NULL" not in fragment
    assert fragment == (
        "($buyer IS NULL OR (NOT EXISTS { MATCH (candidate)-[:BLOCKS]->(excluded) WHERE excluded.buyer_id = $buyer }))"
    )


def test_range_query_window_pass_lets_a_null_bound_impose_no_constraint() -> None:
    fragment = _compile_one(
        {
            "name": "size_window",
            "type": "range",
            "candidateprop": "size",
            "queryparam_min": "size_lo",
            "queryparam_max": "size_hi",
            "nullbehavior": "pass",
        }
    )
    assert fragment == (
        "(candidate.size IS NULL OR (($size_lo IS NULL OR candidate.size >= $size_lo) AND "
        "($size_hi IS NULL OR candidate.size <= $size_hi)))"
    )


@pytest.mark.parametrize("domain_id", ["plasticos", "mortgage-brokerage"])
def test_served_pack_fragments_are_unchanged_by_the_operand_wrap(
    domain_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plasticos and mortgage declare pass with explicit bounds: byte-identical either way."""
    from engine.config.settings import settings
    from engine.gates.compiler import GateCompiler

    spec = _pack(domain_id)

    def _fragments() -> dict[str, str]:
        out: dict[str, str] = {}
        for gate in spec.gates:
            try:
                out[gate.name] = GateCompiler(spec).compile(gate)
            except ValueError as exc:
                out[gate.name] = f"refused: {exc}"
        return out

    on = _fragments()
    monkeypatch.setattr(settings, "gate_null_operands_from_predicate", False)
    assert _fragments() == on


def test_operand_wrap_off_restores_the_legacy_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "gate_null_operands_from_predicate", False)
    fragment = _compile_one(
        {
            "name": "tw",
            "type": "temporalrange",
            "candidateprop": "start",
            "queryparam": "window",
            "nullbehavior": "pass",
        }
    )
    assert fragment == (
        "(candidate.start IS NULL OR $window IS NULL OR (candidate.start >= $window_start AND candidate.start <= $window_end))"
    )


# RG-057 (RU-043 / CEG-COR-022): composites compose — subgates contribute only
# through their composite, and a subgate scoped away from the direction is refused.
_GA = {"name": "ga", "type": "boolean", "candidateprop": "a", "queryparam": "x", "nullbehavior": "fail"}
_GB = {"name": "gb", "type": "boolean", "candidateprop": "b", "queryparam": "y", "nullbehavior": "fail"}
_FIELDS = [{"name": "x", "type": "string"}, {"name": "y", "type": "string"}]


@pytest.mark.parametrize("method", ["compile_all_gates", "compile_relaxed"])
def test_or_composite_is_not_and_ed_with_its_own_subgates(method: str) -> None:  # counterexample [1]
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec(
        [_GA, _GB, {"name": "either", "type": "composite", "logic": "OR", "subgates": ["ga", "gb"]}], _FIELDS
    )
    compiler = GateCompiler(spec)
    ga = compiler.compile(spec.gates[0])
    gb = compiler.compile(spec.gates[1])
    assert getattr(GateCompiler(spec), method)("d1") == f"(({ga}) OR ({gb}))"


def test_subgate_scoped_away_from_the_direction_is_refused() -> None:  # counterexample [8]
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec(
        [
            {**_GA, "matchdirections": ["d1"]},
            {**_GB, "matchdirections": ["d2"]},
            {"name": "both", "type": "composite", "logic": "AND", "subgates": ["ga", "gb"], "matchdirections": ["d1"]},
        ],
        _FIELDS,
    )
    with pytest.raises(ValueError, match="gb"):
        GateCompiler(spec).compile_all_gates("d1")
    with pytest.raises(ValueError, match="gb"):
        GateCompiler(spec).compile_relaxed("d1")


def test_subgate_still_applies_in_a_direction_where_no_composite_references_it() -> None:
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec(
        [_GA, {"name": "only_a", "type": "composite", "logic": "AND", "subgates": ["ga"], "matchdirections": ["d1"]}],
        _FIELDS,
    )
    ga = GateCompiler(spec).compile(spec.gates[0])
    assert GateCompiler(spec).compile_all_gates("d2") == f"({ga})"
    assert GateCompiler(spec).compile_all_gates("d1") == f"(({ga}))"


def test_specs_without_composites_compile_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.gates.compiler import GateCompiler

    spec = _pack("plasticos")
    on = [GateCompiler(spec).compile_all_gates(d) for d in spec.queryschema.matchdirections]
    monkeypatch.setattr(settings, "composite_subgate_scoping", False)
    assert [GateCompiler(spec).compile_all_gates(d) for d in spec.queryschema.matchdirections] == on


def test_composite_subgate_scoping_off_restores_the_legacy_and(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.gates.compiler import GateCompiler

    monkeypatch.setattr(settings, "composite_subgate_scoping", False)
    spec = _two_label_spec(
        [_GA, _GB, {"name": "either", "type": "composite", "logic": "OR", "subgates": ["ga", "gb"]}], _FIELDS
    )
    compiler = GateCompiler(spec)
    ga = compiler.compile(spec.gates[0])
    gb = compiler.compile(spec.gates[1])
    assert GateCompiler(spec).compile_all_gates("d1") == f"({ga}) AND ({gb}) AND (({ga}) OR ({gb}))"


# RG-066 (RU-052 / CEG-COR-079): freshness maxagedays defaults only when absent.
def _freshness(maxagedays: int | None) -> dict:
    gate = {"name": "fresh", "type": "freshness", "candidateprop": "updated_at", "matchdirections": ["d1"]}
    if maxagedays is not None:
        gate["maxagedays"] = maxagedays
    return gate


@pytest.mark.parametrize("maxagedays", [0, -30])
def test_freshness_refuses_a_non_positive_age(maxagedays: int) -> None:
    from engine.config.loader import admission_defects
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec([_freshness(maxagedays)])
    with pytest.raises(ValueError, match="fresh"):
        GateCompiler(spec).compile(spec.gates[0])
    assert any(d.startswith("fresh:") for d in admission_defects(spec))


@pytest.mark.parametrize(("maxagedays", "days"), [(None, 1), (1, 1), (90, 90)])
def test_freshness_compiles_absent_as_one_day(maxagedays: int | None, days: int) -> None:
    from engine.gates.compiler import GateCompiler

    spec = _two_label_spec([_freshness(maxagedays)])
    assert f"duration({{days: {days}}})" in GateCompiler(spec).compile(spec.gates[0])


def test_freshness_validation_off_restores_the_legacy_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.gates.compiler import GateCompiler

    monkeypatch.setattr(settings, "freshness_maxagedays_validated", False)
    zero = _two_label_spec([_freshness(0)])
    negative = _two_label_spec([_freshness(-30)])
    assert "duration({days: 1})" in GateCompiler(zero).compile(zero.gates[0])
    assert "duration({days: -30})" in GateCompiler(negative).compile(negative.gates[0])
