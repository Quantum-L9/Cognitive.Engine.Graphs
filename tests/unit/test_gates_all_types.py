"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gates]
owner: engine-team
status: active
--- /L9_META ---

Comprehensive unit tests for gate types and GateCompiler.
Target Coverage: 85%+
"""

from unittest.mock import MagicMock

import pytest

from engine.config.schema import DomainSpec, GateSpec, GateType, NullBehavior
from engine.gates.compiler import GateCompiler
from engine.gates.types.all_gates import (
    BaseGate,
    BooleanGate,
    EnumMapGate,
    ExclusionGate,
    FreshnessGate,
    ThresholdGate,
    TraversalGate,
)


def make_mock_gate_spec(**kwargs) -> MagicMock:
    """Create a mock GateSpec with given attributes.

    Uses camelCase field names to match GateSpec's actual Pydantic field names.
    """
    spec = MagicMock(spec=GateSpec)
    spec.name = kwargs.get("name", "test_gate")
    # .type is what GateCompiler reads; gate_type kwarg is the caller-facing alias
    spec.type = kwargs.get("gate_type", kwargs.get("type", GateType.THRESHOLD))
    # camelCase fields matching GateSpec.model_fields
    spec.candidateprop = kwargs.get("candidate_prop") or kwargs.get("candidateprop")
    spec.queryparam = kwargs.get("query_param") or kwargs.get("queryparam")
    spec.nullbehavior = kwargs.get("null_behavior", kwargs.get("nullbehavior", NullBehavior.PASS))
    spec.matchdirections = kwargs.get("match_directions") or kwargs.get("matchdirections")
    spec.roleexempt = kwargs.get("exempt_roles") or kwargs.get("roleexempt")
    spec.relaxedpenalty = kwargs.get("relaxed_penalty") or kwargs.get("relaxedpenalty")
    spec.candidateprop_min = kwargs.get("candidate_prop_min") or kwargs.get("candidateprop_min")
    spec.candidateprop_max = kwargs.get("candidate_prop_max") or kwargs.get("candidateprop_max")
    spec.invertible = kwargs.get("inverse", kwargs.get("invertible", False))
    spec.edgetype = kwargs.get("edge_type") or kwargs.get("edgetype")
    spec.fromnode = kwargs.get("from_node") or kwargs.get("fromnode")
    spec.tonode = kwargs.get("to_node") or kwargs.get("tonode")
    spec.subgates = kwargs.get("sub_gates") or kwargs.get("subgates")
    spec.logic = kwargs.get("combinator") or kwargs.get("logic", "AND")
    spec.maxagedays = kwargs.get("max_age_days") or kwargs.get("maxagedays")
    spec.operator = kwargs.get("operator")
    spec.queryparam_start = kwargs.get("query_param_start") or kwargs.get("queryparam_start")
    spec.queryparam_end = kwargs.get("query_param_end") or kwargs.get("queryparam_end")
    spec.cypheroverride = kwargs.get("cypher_override") or kwargs.get("cypheroverride")
    spec.pattern = kwargs.get("pattern")
    spec.condition = kwargs.get("condition")
    return spec


def make_mock_domain_spec(gates: list | None = None) -> MagicMock:
    """Create a mock DomainSpec."""
    spec = MagicMock(spec=DomainSpec)
    spec.gates = gates or []  # compiler iterates domain_spec.gates directly
    spec.compliance = None
    return spec


# ============================================================================
# GATE COMPILER TESTS
# ============================================================================


@pytest.mark.unit
class TestGateCompiler:
    """Test GateCompiler functionality."""

    def test_compile_threshold_gate(self) -> None:
        """Threshold gate compiles to >= comparison."""
        gate = make_mock_gate_spec(
            name="credit_min",
            gate_type=GateType.THRESHOLD,
            candidate_prop="mincreditscore",
            query_param="creditscore",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "candidate.mincreditscore" in cypher
        assert "$creditscore" in cypher

    def test_compile_boolean_gate(self) -> None:
        """Boolean gate compiles to equality check."""
        gate = make_mock_gate_spec(
            name="va_eligible",
            gate_type=GateType.BOOLEAN,
            candidate_prop="acceptsva",
            query_param="vaeligible",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "candidate.acceptsva" in cypher

    def test_compile_range_gate(self) -> None:
        """Range gate compiles to min/max bounds."""
        gate = make_mock_gate_spec(
            name="price_range",
            gate_type=GateType.RANGE,
            candidate_prop="price",
            query_param="price",
            candidate_prop_min="min_price",
            candidate_prop_max="max_price",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "candidate.min_price" in cypher or "min_price" in cypher

    def test_compile_enummap_gate(self) -> None:
        """EnumMap gate compiles to IN clause."""
        gate = make_mock_gate_spec(
            name="polymer_match",
            gate_type=GateType.ENUMMAP,
            candidate_prop="polymertype",
            query_param="polymertype",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "IN" in cypher or "candidate.polymertype" in cypher

    def test_compile_exclusion_gate(self) -> None:
        """Exclusion gate compiles to NOT exists pattern."""
        gate = make_mock_gate_spec(
            name="blacklist",
            gate_type=GateType.EXCLUSION,
            edge_type="BLACKLISTED",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "NOT" in cypher
        assert "exists" in cypher.lower()

    def test_compile_selfrange_gate(self) -> None:
        """SelfRange gate compiles to candidate min/max bounds."""
        gate = make_mock_gate_spec(
            name="capacity",
            gate_type=GateType.SELFRANGE,
            candidate_prop="capacity",
            query_param="requested",
            candidate_prop_min="min_capacity",
            candidate_prop_max="max_capacity",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "candidate" in cypher

    def test_compile_freshness_gate(self) -> None:
        """Freshness gate compiles to datetime comparison."""
        gate = make_mock_gate_spec(
            name="data_fresh",
            gate_type=GateType.FRESHNESS,
            candidate_prop="lastupdated",
            duration_field="days",
            duration_value=7,
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "datetime()" in cypher
        assert "duration" in cypher

    def test_compile_temporalrange_gate(self) -> None:
        """TemporalRange gate compiles to date range check."""
        gate = make_mock_gate_spec(
            name="availability",
            gate_type=GateType.TEMPORALRANGE,
            candidate_prop="available_date",
            query_param="target_date",
            query_param_start="start_date",
            query_param_end="end_date",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "candidate.available_date" in cypher

    def test_compile_traversal_gate(self) -> None:
        """Traversal gate compiles to exists pattern."""
        gate = make_mock_gate_spec(
            name="network",
            gate_type=GateType.TRAVERSAL,
            edge_type="ACCEPTS",
            target_label="Insurance",
            target_prop="planid",
            query_param="insuranceplan",
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "exists" in cypher.lower()

    def test_compile_all_gates_combines_with_and(self) -> None:
        """compile_all_gates combines gates with AND."""
        gate1 = make_mock_gate_spec(
            name="gate1",
            gate_type=GateType.BOOLEAN,
            candidate_prop="prop1",
            query_param="val1",
        )
        gate2 = make_mock_gate_spec(
            name="gate2",
            gate_type=GateType.BOOLEAN,
            candidate_prop="prop2",
            query_param="val2",
        )
        domain_spec = make_mock_domain_spec(gates=[gate1, gate2])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile_all_gates(match_direction="buyer_to_seller")

        assert "AND" in cypher

    def test_compile_all_gates_filters_by_direction(self) -> None:
        """compile_all_gates filters gates by match direction."""
        gate1 = make_mock_gate_spec(
            name="gate1",
            gate_type=GateType.BOOLEAN,
            candidate_prop="prop1",
            query_param="val1",
            match_directions=["buyer_to_seller"],
        )
        gate2 = make_mock_gate_spec(
            name="gate2",
            gate_type=GateType.BOOLEAN,
            candidate_prop="prop2",
            query_param="val2",
            match_directions=["seller_to_buyer"],
        )
        domain_spec = make_mock_domain_spec(gates=[gate1, gate2])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile_all_gates(match_direction="buyer_to_seller")

        assert "prop1" in cypher
        assert "prop2" not in cypher

    def test_compile_all_gates_exempts_by_role(self) -> None:
        """compile_all_gates exempts gates by role."""
        gate = make_mock_gate_spec(
            name="gate1",
            gate_type=GateType.BOOLEAN,
            candidate_prop="prop1",
            query_param="val1",
            exempt_roles=["admin"],
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile_all_gates(match_direction="any", role="admin")

        assert cypher == "true"  # Gate exempted

    def test_compile_all_gates_empty_returns_true(self) -> None:
        """compile_all_gates returns 'true' when no gates."""
        domain_spec = make_mock_domain_spec(gates=[])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile_all_gates(match_direction="any")

        assert cypher == "true"


# ============================================================================
# NULL SEMANTICS TESTS
# ============================================================================


@pytest.mark.unit
class TestNullSemantics:
    """Test NULL behavior in gate compilation."""

    def test_null_pass_wraps_with_or_null(self) -> None:
        """NullBehavior.PASS wraps predicate with OR IS NULL."""
        gate = make_mock_gate_spec(
            name="test",
            gate_type=GateType.THRESHOLD,
            candidate_prop="prop",
            query_param="val",
            null_behavior=NullBehavior.PASS,
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "IS NULL" in cypher or "OR" in cypher

    def test_null_fail_wraps_with_and_not_null(self) -> None:
        """NullBehavior.FAIL wraps predicate with AND IS NOT NULL."""
        gate = make_mock_gate_spec(
            name="test",
            gate_type=GateType.THRESHOLD,
            candidate_prop="prop",
            query_param="val",
            null_behavior=NullBehavior.FAIL,
        )
        domain_spec = make_mock_domain_spec(gates=[gate])
        compiler = GateCompiler(domain_spec)

        cypher = compiler.compile(gate)

        assert "IS NOT NULL" in cypher or "AND" in cypher


# ============================================================================
# GATE TYPE CLASS TESTS (all_gates.py)
# ============================================================================


@pytest.mark.unit
class TestGateTypeClasses:
    """Test individual gate type classes from all_gates.py."""

    def test_base_gate_is_abstract(self) -> None:
        """BaseGate cannot be instantiated directly."""
        spec = MagicMock()
        domain_spec = MagicMock()
        with pytest.raises(TypeError):
            BaseGate(spec, domain_spec)

    def test_threshold_gate_compile(self) -> None:
        """ThresholdGate.compile() generates comparison."""
        spec = MagicMock()
        spec.name = "test"
        spec.candidateprop = "mincredit"
        spec.queryparam = "credit"
        spec.operator = "<="

        domain_spec = MagicMock()
        gate = ThresholdGate(spec, domain_spec)

        cypher = gate.compile()

        assert "candidate.mincredit" in cypher
        assert "<=" in cypher

    def test_boolean_gate_compile(self) -> None:
        """BooleanGate.compile() generates equality."""
        spec = MagicMock()
        spec.name = "test"
        spec.candidateprop = "active"
        spec.queryparam = "isactive"

        domain_spec = MagicMock()
        gate = BooleanGate(spec, domain_spec)

        cypher = gate.compile()

        assert "candidate.active" in cypher
        assert "=" in cypher

    def test_exclusion_gate_compile(self) -> None:
        """ExclusionGate.compile() generates NOT EXISTS."""
        spec = MagicMock()
        spec.name = "test"
        spec.edgetype = "BLOCKED"
        spec.fromnode = None
        spec.tonode = None

        domain_spec = MagicMock()
        gate = ExclusionGate(spec, domain_spec)

        cypher = gate.compile()

        assert "NOT EXISTS" in cypher
        assert "BLOCKED" in cypher

    def test_freshness_gate_compile(self) -> None:
        """FreshnessGate.compile() generates duration check."""
        spec = MagicMock()
        spec.name = "test"
        spec.candidateprop = "updated_at"
        spec.maxagedays = 30

        domain_spec = MagicMock()
        gate = FreshnessGate(spec, domain_spec)

        cypher = gate.compile()

        assert "duration.between" in cypher
        assert "30" in cypher

    def test_traversal_gate_compile(self) -> None:
        """TraversalGate.compile() generates EXISTS pattern."""
        spec = MagicMock()
        spec.name = "test"
        spec.pattern = "(candidate)-[:HAS]->(t:Target)"
        spec.condition = "t.id = $param"

        domain_spec = MagicMock()
        gate = TraversalGate(spec, domain_spec)

        cypher = gate.compile()

        assert "EXISTS" in cypher
        assert "HAS" in cypher


# ============================================================================
# C-009: GATE VALUES TRAVEL AS PARAMETERS, IDENTIFIERS ARE VALIDATED
# ============================================================================


@pytest.mark.unit
class TestGateValueParameterization:
    """F280-1: enum mapping keys/values are data, never quoted into the fragment."""

    @staticmethod
    def _enum_gate(mapping: dict) -> EnumMapGate:
        spec = MagicMock()
        spec.name = "polymer-map"
        spec.candidateprop = "polymertype"
        spec.queryparam = "polymertype"
        spec.mapping = mapping
        return EnumMapGate(spec, MagicMock())

    def test_mapping_values_are_bound_as_parameters(self) -> None:
        gate = self._enum_gate({"PET": ["PET", "rPET"], "HDPE": ["HDPE"]})

        cypher = gate.compile()
        params = gate.query_params

        assert cypher == (
            "CASE WHEN $query.polymertype = $gate_polymer_map_key_0 THEN candidate.polymertype IN $gate_polymer_map_values_0 "
            "WHEN $query.polymertype = $gate_polymer_map_key_1 THEN candidate.polymertype IN $gate_polymer_map_values_1 "
            "ELSE false END"
        )
        assert params == {
            "gate_polymer_map_key_0": "PET",
            "gate_polymer_map_values_0": ["PET", "rPET"],
            "gate_polymer_map_key_1": "HDPE",
            "gate_polymer_map_values_1": ["HDPE"],
        }
        assert "'" not in cypher

    def test_legitimate_non_identifier_values_are_accepted(self) -> None:
        """Spaces and hyphens are valid data — sanitize_label would have rejected them."""
        gate = self._enum_gate({"post-consumer": ["post consumer", "post-industrial", "mixed/bale"]})

        cypher = gate.compile()

        assert "post-consumer" not in cypher
        assert gate.query_params["gate_polymer_map_key_0"] == "post-consumer"
        assert gate.query_params["gate_polymer_map_values_0"] == ["post consumer", "post-industrial", "mixed/bale"]

    def test_injection_shaped_values_never_reach_the_fragment(self) -> None:
        payload = "x' OR 1=1 OR '"
        gate = self._enum_gate({payload: ["a'] OR true OR ['b"]})

        cypher = gate.compile()

        assert "OR 1=1" not in cypher
        assert "OR true" not in cypher
        assert gate.query_params["gate_polymer_map_key_0"] == payload

    def test_params_reset_between_compiles(self) -> None:
        gate = self._enum_gate({"PET": ["PET"]})
        gate.compile()
        gate.spec.mapping = {"HDPE": ["HDPE"]}
        gate.compile()
        assert gate.query_params == {"gate_polymer_map_key_0": "HDPE", "gate_polymer_map_values_0": ["HDPE"]}

    def test_no_mapping_binds_nothing(self) -> None:
        gate = self._enum_gate({})
        assert gate.compile() == "$query.polymertype IN candidate.polymertype"
        assert gate.query_params == {}

    def test_parameter_keys_stay_bounded_identifiers_for_long_gate_names(self) -> None:
        gate = self._enum_gate({"PET": ["PET"]})
        gate.spec.name = "g" * 120
        gate.compile()
        for key in gate.query_params:
            assert len(key) <= 64
            assert key.replace("_", "a").isalnum()

    def test_composite_gate_collects_subgate_parameters(self) -> None:
        from engine.config.schema import GateType
        from engine.gates.types.all_gates import CompositeGate

        sub = MagicMock()
        sub.name = "sub"
        sub.type = GateType.ENUMMAP
        sub.candidateprop = "p"
        sub.queryparam = "q"
        sub.mapping = {"a": ["b"]}
        composite = MagicMock()
        composite.name = "both"
        composite.subgates = ["sub"]
        composite.logic = "and"
        domain_spec = MagicMock()
        domain_spec.gates = [sub]

        gate = CompositeGate(composite, domain_spec)
        cypher = gate.compile()

        assert " AND " in cypher or cypher.count("(") == 1
        assert gate.query_params == {"gate_sub_key_0": "a", "gate_sub_values_0": ["b"]}

    def test_composite_logic_outside_allow_list_is_rejected(self) -> None:
        from engine.gates.types.all_gates import CompositeGate

        composite = MagicMock()
        composite.name = "bad"
        composite.subgates = ["x"]
        composite.logic = "AND true OR"
        domain_spec = MagicMock()
        domain_spec.gates = []
        with pytest.raises((KeyError, ValueError)):
            CompositeGate(composite, domain_spec).compile()


@pytest.mark.unit
class TestGateIdentifierValidation:
    """Structural identifiers read from the spec are sanitized before interpolation."""

    def test_exclusion_gate_sanitizes_edge_and_node_variables(self) -> None:
        spec = MagicMock()
        spec.name = "blocked"
        spec.edgetype = "BLOCKED"
        spec.fromnode = "q"
        spec.tonode = "c"
        assert ExclusionGate(spec, MagicMock()).compile() == "NOT EXISTS((q)-[:BLOCKED]->(c))"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("edgetype", "BLOCKED]->() RETURN 1 //"),
            ("fromnode", "query) WHERE true OR (x"),
            ("tonode", "candidate)) OR true OR (("),
        ],
    )
    def test_exclusion_gate_rejects_injection_shaped_identifiers(self, field: str, value: str) -> None:
        spec = MagicMock()
        spec.name = "blocked"
        spec.edgetype = "BLOCKED"
        spec.fromnode = None
        spec.tonode = None
        setattr(spec, field, value)
        with pytest.raises(ValueError):
            ExclusionGate(spec, MagicMock()).compile()

    def test_threshold_operator_outside_allow_list_is_rejected(self) -> None:
        spec = MagicMock()
        spec.name = "t"
        spec.candidateprop = "score"
        spec.queryparam = "score"
        spec.operator = ">= 0 OR 1=1 OR candidate.score"
        with pytest.raises(KeyError):
            ThresholdGate(spec, MagicMock()).compile()

    def test_prop_and_param_refs_validate_identifiers(self) -> None:
        spec = MagicMock()
        spec.name = "b"
        spec.candidateprop = "candidate.active"
        spec.queryparam = "$query.active"
        assert BooleanGate(spec, MagicMock()).compile() == "candidate.active = $query.active"

        spec.candidateprop = "active) OR true OR (x"
        with pytest.raises(ValueError):
            BooleanGate(spec, MagicMock()).compile()
        spec.candidateprop = "active"
        spec.queryparam = "active RETURN 1"
        with pytest.raises(ValueError):
            BooleanGate(spec, MagicMock()).compile()

    def test_gate_compiler_validates_query_parameter_names(self) -> None:
        gate = make_mock_gate_spec(
            name="credit_min",
            gate_type=GateType.THRESHOLD,
            candidate_prop="mincreditscore",
            query_param="creditscore RETURN 1 //",
            operator=">=",
        )
        compiler = GateCompiler(make_mock_domain_spec(gates=[gate]))
        with pytest.raises(ValueError):
            compiler.compile(gate)

    def test_gate_compiler_validates_operator_and_logic(self) -> None:
        threshold = make_mock_gate_spec(
            name="t", gate_type=GateType.THRESHOLD, candidate_prop="score", query_param="score", operator="=="
        )
        compiler = GateCompiler(make_mock_domain_spec(gates=[threshold]))
        with pytest.raises(KeyError):
            compiler.compile(threshold)

        composite = make_mock_gate_spec(name="c", gate_type=GateType.COMPOSITE, sub_gates=["t"], combinator="AND OR")
        threshold.operator = ">="
        compiler = GateCompiler(make_mock_domain_spec(gates=[threshold, composite]))
        with pytest.raises(KeyError):
            compiler.compile(composite)
