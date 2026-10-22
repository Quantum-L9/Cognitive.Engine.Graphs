"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gates, strictwhen]
owner: engine-team
status: active
--- /L9_META ---

CEG-009c — `strictwhen` is consumed by the compiler.

`GateSpec.strictwhen` was declared by the schema and read by nothing under
engine/, so a gate its author wrote as conditional ran as an unconditional
hard filter. `GateCompiler.compile` now wraps the gate:

    (NOT coalesce(<condition>, false) OR (<gate>))

The condition is parsed against the compiler's expression grammar — never
interpolated, never eval'd — and every literal in it is bound as a generated
parameter. Composite gates compile subgates through `compile`, so a subgate's
`strictwhen` applies inside a composite exactly as it does alone.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.config.schema import DomainSpec, GateSpec, GateType, NullBehavior
from engine.gates.compiler import GateCompiler


def _gate(**kwargs) -> GateSpec:
    kwargs.setdefault("name", "g")
    kwargs.setdefault("type", GateType.BOOLEAN)
    kwargs.setdefault("candidateprop", "flag")
    return GateSpec(**kwargs)


def _compiler(*gates: GateSpec) -> GateCompiler:
    domain = MagicMock(spec=DomainSpec)
    domain.gates = list(gates)
    domain.compliance = None
    return GateCompiler(domain)


INNER = "(candidate.flag IS NOT NULL AND $requirescode IS NOT NULL AND (candidate.flag = $requirescode))"


@pytest.mark.unit
class TestStrictWhenCompilation:
    def test_dollar_query_reference_with_string_literal(self) -> None:
        gate = _gate(
            name="emergency_availability",
            candidateprop="emergencyavailable",
            queryparam=True,
            strictwhen="$query.urgency = 'emergency'",
        )
        compiler = _compiler(gate)

        cypher = compiler.compile(gate)

        assert cypher == (
            "(NOT coalesce($urgency = $gate_emergency_availability_strictwhen_0, false) OR "
            "((candidate.emergencyavailable IS NOT NULL AND "
            "(candidate.emergencyavailable = $gate_emergency_availability_value))))"
        )
        assert compiler.last_query_params == {
            "gate_emergency_availability_strictwhen_0": "emergency",
            "gate_emergency_availability_value": True,
        }

    def test_boolean_literal_in_condition(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="$query.requirescode = true")
        compiler = _compiler(gate)

        assert compiler.compile(gate) == f"(NOT coalesce($requirescode = $gate_g_strictwhen_0, false) OR ({INNER}))"
        assert compiler.last_query_params == {"gate_g_strictwhen_0": True}

    def test_false_literal_and_bare_query_prefix(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="query.includeprivileged = false")
        compiler = _compiler(gate)

        assert compiler.compile(gate).startswith("(NOT coalesce($includeprivileged = $gate_g_strictwhen_0, false) OR (")
        assert compiler.last_query_params["gate_g_strictwhen_0"] is False

    def test_numbers_bind_with_their_type(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="$budget >= 2500 AND $ratio < 0.5")
        compiler = _compiler(gate)

        cypher = compiler.compile(gate)

        assert "($budget >= $gate_g_strictwhen_0 AND $ratio < $gate_g_strictwhen_1)" in cypher
        assert compiler.last_query_params == {"gate_g_strictwhen_0": 2500, "gate_g_strictwhen_1": 0.5}

    def test_boolean_structure_not_or_and_parentheses_null_tests(self) -> None:
        gate = _gate(
            queryparam="requirescode",
            strictwhen="NOT ($mode <> 'audit' OR $mode IS NULL) AND candidate.tier IS NOT NULL",
        )
        compiler = _compiler(gate)

        cypher = compiler.compile(gate)

        assert cypher.startswith(
            "(NOT coalesce((NOT (($mode <> $gate_g_strictwhen_0 OR $mode IS NULL)) AND candidate.tier IS NOT NULL), false) OR ("
        )

    def test_keywords_are_case_insensitive_and_whitespace_free(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="  $a=1 and $b=2  ")
        compiler = _compiler(gate)
        assert "($a = $gate_g_strictwhen_0 AND $b = $gate_g_strictwhen_1)" in compiler.compile(gate)

    def test_blank_strictwhen_is_absent(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="   ")
        assert _compiler(gate).compile(gate) == INNER

    def test_no_strictwhen_leaves_the_gate_untouched(self) -> None:
        gate = _gate(queryparam="requirescode")
        assert _compiler(gate).compile(gate) == INNER


@pytest.mark.unit
class TestStrictWhenGrammarRefusals:
    @pytest.mark.parametrize(
        "condition",
        [
            "$query.urgency = 'emergency' OR 1=1 //",
            "$x = 1; MATCH (n) DETACH DELETE n",
            "$x = 'a' RETURN 1",
            "candidate = 1",  # bare bound node compared
            "query IS NOT NULL",  # query is not a node
            "other.prop = 1",  # unknown variable
            "$x = null",
            "null IS NULL",
            "$x",
            "$x =",
            "= 1",
            "($x = 1",
            "$x = 1)",
            "$x == 1",
            '$x = "double"',
            "$x = 'unterminated",
            "$x IN [1, 2]",
            "$x CONTAINS 'a'",
            "$x = 1 AND",
            "NOT",
        ],
    )
    def test_outside_the_grammar_is_refused_with_the_gate_named(self, condition: str) -> None:
        gate = _gate(queryparam="requirescode", strictwhen=condition)
        with pytest.raises(ValueError, match="Gate 'g': strictwhen .* is not accepted"):
            _compiler(gate).compile(gate)

    def test_length_bound(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen=" OR ".join(["$a = 1"] * 200))
        with pytest.raises(ValueError, match="exceeds 512 characters"):
            _compiler(gate).compile(gate)

    def test_depth_bound(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="NOT " * 40 + "$a = 1")
        with pytest.raises(ValueError, match="nested deeper"):
            _compiler(gate).compile(gate)

    def test_injection_shaped_string_literal_is_bound_not_emitted(self) -> None:
        gate = _gate(queryparam="requirescode", strictwhen="$mode = 'x OR 1=1 OR '")
        compiler = _compiler(gate)

        cypher = compiler.compile(gate)

        assert "OR 1=1" not in cypher
        assert compiler.last_query_params["gate_g_strictwhen_0"] == "x OR 1=1 OR "

    def test_no_eval_in_the_grammar_path(self) -> None:
        import inspect

        import engine.gates.compiler as module

        source = inspect.getsource(module)
        for banned in ("eval(", "exec(", "compile(source", "ast.literal_eval"):
            assert banned not in source, banned


@pytest.mark.unit
class TestStrictWhenThroughComposites:
    def test_subgate_strictwhen_is_inherited_inside_a_composite(self) -> None:
        leaf = _gate(name="leaf", queryparam="requirescode", strictwhen="$query.requirescode = true")
        plain = _gate(name="plain", candidateprop="other", queryparam="other")
        composite = GateSpec(name="both", type=GateType.COMPOSITE, subgates=["leaf", "plain"], logic="AND")
        compiler = _compiler(leaf, plain, composite)

        cypher = compiler.compile(composite)

        assert "(NOT coalesce($requirescode = $gate_leaf_strictwhen_0, false) OR (" in cypher
        assert cypher.count("coalesce(") == 1
        assert compiler.last_query_params == {"gate_leaf_strictwhen_0": True}

    def test_composite_own_strictwhen_wraps_the_whole_combination(self) -> None:
        a = _gate(name="a", candidateprop="pa", queryparam="qa")
        b = _gate(name="b", candidateprop="pb", queryparam="qb")
        composite = GateSpec(
            name="either", type=GateType.COMPOSITE, subgates=["a", "b"], logic="OR", strictwhen="$strict = true"
        )
        compiler = _compiler(a, b, composite)

        cypher = compiler.compile(composite)

        assert cypher.startswith("(NOT coalesce($strict = $gate_either_strictwhen_0, false) OR (")
        assert " OR " in cypher
        assert compiler.last_query_params == {"gate_either_strictwhen_0": True}

    def test_compile_all_gates_carries_strictwhen_literals(self) -> None:
        leaf = _gate(name="leaf", queryparam="requirescode", strictwhen="$query.requirescode = true")
        compiler = _compiler(leaf)

        where = compiler.compile_all_gates("any")

        assert where.startswith("((NOT coalesce(")
        assert compiler.last_query_params == {"gate_leaf_strictwhen_0": True}


@pytest.mark.unit
class TestStrictWhenNullSemantics:
    def test_null_behaviour_is_applied_inside_the_wrapper(self) -> None:
        gate = _gate(queryparam="requirescode", nullbehavior=NullBehavior.PASS, strictwhen="$strict = true")
        cypher = _compiler(gate).compile(gate)
        # BOOLEAN defaults to FAIL even when PASS is declared (NullHandler default); the wrapper
        # sits outside whatever the null handler produced.
        assert cypher.endswith(f"OR ({INNER}))")
