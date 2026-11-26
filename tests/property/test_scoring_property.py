"""
Property-based tests for scoring (W5-05; rewritten by CEG scoring audit L2).

Every property drives repository code: ``ScoringAssembler`` assembles the
scoring clause from generated request and learned weights (``cypher_number``'s
own properties live with its non-finite refusal, in
``tests/unit/test_scoring_spec_strict_validation.py``). The earlier cases asserted
properties of their own generators and of a test-local ``clamp`` and so could
not fail on any change to the engine.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.config.schema import DomainSpec
from engine.scoring.assembler import ScoringAssembler
from engine.utils.security import cypher_number
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

_DIRECTION = "window_to_depot"
_RAW = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text(encoding="utf-8"))


def _spec(feedback: bool) -> DomainSpec:
    raw = copy.deepcopy(_RAW)
    if feedback:
        raw["feedbackloop"] = {**raw.get("feedbackloop", {}), "enabled": True, "signal_weights": {"enabled": True}}
    return DomainSpec.model_validate(raw)


_SPEC = _spec(feedback=False)
_FEEDBACK_SPEC = _spec(feedback=True)


class _RowsDriver:
    """A fixed DimensionWeight read (unit tier: no Neo4j)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        return self.rows


unit_weight = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
any_learned = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(min_value=-5.0, max_value=5.0, allow_nan=False, allow_infinity=False),
    st.none(),
)


def _assemble(rows: list[dict[str, Any]], request: dict[str, float]) -> tuple[ScoringAssembler, str]:
    assembler = ScoringAssembler(_FEEDBACK_SPEC, graph_driver=_RowsDriver(rows))  # type: ignore[arg-type]
    asyncio.run(assembler.load_learned_weights())
    clause, _ = assembler.assemble_scoring_clause(_DIRECTION, request)
    return assembler, clause


def _nonzero(**weights: float) -> list[str]:
    """SCR-01: the dimensions a request keeps, in spec order (a 0 weight excludes)."""
    return [name for name, weight in weights.items() if weight]


class TestScoringProperties:
    @given(w_rel=unit_weight, w_ports=unit_weight, l_rel=any_learned, l_ports=any_learned)
    @settings(max_examples=150, deadline=None)
    def test_emitted_coefficients_are_finite_and_the_composite_is_clamped(
        self, w_rel: float, w_ports: float, l_rel: Any, l_ports: Any
    ) -> None:
        rows = [{"name": "depot_reliability", "weight": l_rel}, {"name": "port_capacity", "weight": l_ports}]
        assembler, clause = _assemble(rows, {"w_reliability": w_rel, "w_ports": w_ports})
        weights = assembler.last_effective_weights
        # SCR-01: a dimension with a 0 request weight is excluded, learning or not.
        active = _nonzero(depot_reliability=w_rel, port_capacity=w_ports)
        assert list(weights) == active
        assert all(math.isfinite(w) for w in weights.values())
        score_line = clause.splitlines()[-1]
        if not active:
            assert score_line == "WITH candidate, 0.0 AS score"
            return
        assert score_line.startswith(f"WITH candidate, {', '.join(active)}, CASE WHEN ((")
        assert score_line.endswith(" END AS score")
        assert "isNaN(" in score_line

    @given(w_rel=unit_weight, w_ports=unit_weight, l_rel=any_learned, l_ports=any_learned)
    @settings(max_examples=150, deadline=None)
    def test_learning_never_raises_the_positive_weight_sum(
        self, w_rel: float, w_ports: float, l_rel: Any, l_ports: Any
    ) -> None:
        """RU-023: positive coefficients sum to at most the request weights' sum; a penalty is negative."""
        rows = [{"name": "depot_reliability", "weight": l_rel}, {"name": "port_capacity", "weight": l_ports}]
        assembler, _ = _assemble(rows, {"w_reliability": w_rel, "w_ports": w_ports})
        positive = sum(w for w in assembler.last_effective_weights.values() if w > 0)
        assert positive <= w_rel + w_ports + 1e-9

    @given(w_rel=unit_weight, w_ports=unit_weight)
    @settings(max_examples=100, deadline=None)
    def test_request_weights_are_emitted_verbatim_without_learning(self, w_rel: float, w_ports: float) -> None:
        assembler = ScoringAssembler(_SPEC)
        clause, _ = assembler.assemble_scoring_clause(_DIRECTION, {"w_reliability": w_rel, "w_ports": w_ports})
        requested = {"depot_reliability": w_rel, "port_capacity": w_ports}
        active = _nonzero(**requested)
        assert assembler.last_effective_weights == {name: requested[name] for name in active}
        if "depot_reliability" in active:
            assert f"({cypher_number(w_rel)} * depot_reliability)" in clause

    @given(expr=st.from_regex(re.compile(r"[a-z_]{1,8}(\.[a-z_]{1,8})?"), fullmatch=True))
    @settings(max_examples=50)
    def test_clamp_expression_wraps_any_operand(self, expr: str) -> None:
        clamped = ScoringAssembler._clamp_expression(expr)
        assert clamped == (
            f"CASE WHEN ({expr}) IS NULL THEN 0.0 WHEN isNaN(({expr})) THEN 0.0 "
            f"WHEN ({expr}) < 0.0 THEN 0.0 WHEN ({expr}) > 1.0 THEN 1.0 ELSE ({expr}) END"
        )
        assert clamped.count("(") == clamped.count(")")

    def test_empty_scoring_emits_a_zero_score(self) -> None:
        raw = copy.deepcopy(_RAW)
        raw["scoring"]["dimensions"] = []
        clause, _ = ScoringAssembler(DomainSpec.model_validate(raw)).assemble_scoring_clause(_DIRECTION, {})
        assert clause == "WITH candidate, 0.0 AS score"
