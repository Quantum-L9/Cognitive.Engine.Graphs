"""Integration — pricealignment with INTEGER-stored operands (RU-017 / CEG-COR-006, RG-022).

Cypher divides two INTEGER values as INTEGER: ``450 / 500`` is ``0``, so the
legacy ``log(candidate.p / $q)`` was ``log(0)`` and the dimension clamped to
0.0. With ``pricealignment_float_division`` on, both operands are cast with
``toFloat()`` and the ratio 0.9 scores ``1 - |ln 0.9| / 2 ~= 0.947``.

The compiled expression is evaluated against a real Neo4j node; no domain
database is needed, so the default ``neo4j`` database is used.
"""

from __future__ import annotations

import math

import pytest

from engine.config.schema import ComputationType, ScoringDimensionSpec, ScoringSource

_LABEL = "_PriceAlignmentProbe"
_EXPECTED = 1.0 - abs(math.log(450 / 500)) / 2.0  # ~0.9473


def _dimension() -> ScoringDimensionSpec:
    return ScoringDimensionSpec(
        name="price_alignment",
        source=ScoringSource.CANDIDATEPROPERTY,
        computation=ComputationType.PRICEALIGNMENT,
        candidateprop="rate_per_ton",
        queryprop="target_price",
        weightkey="w_price",
        defaultweight=0.15,
    )


async def _score(graph_driver, domain_spec) -> float:
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(domain_spec)
    expr = assembler._clamp_expression(assembler._compile_pricealignment(_dimension()))
    await graph_driver.execute_query(
        f"CREATE (candidate:{_LABEL} {{rate_per_ton: $rate}})",
        parameters={"rate": 450},  # Python int -> Cypher INTEGER
        database="neo4j",
    )
    try:
        rows = await graph_driver.execute_query(
            f"MATCH (candidate:{_LABEL}) RETURN {expr} AS price_alignment",
            parameters={"target_price": 500},  # INTEGER parameter
            database="neo4j",
        )
    finally:
        await graph_driver.execute_query(f"MATCH (n:{_LABEL}) DETACH DELETE n", parameters={}, database="neo4j")
    return float(rows[0]["price_alignment"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integer_operands_score_float_ratio(graph_driver, domain_spec):
    """rate_per_ton=450 (INTEGER), $target_price=500 (INTEGER) -> ~0.947, not 0.0."""
    value = await _score(graph_driver, domain_spec)
    assert value == pytest.approx(_EXPECTED, abs=1e-3)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flag_off_reproduces_integer_truncation(graph_driver, domain_spec, monkeypatch):
    """C-21 rollback: the legacy expression truncates 450/500 to 0 -> log(0) -> clamped 0.0."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "pricealignment_float_division", False)
    value = await _score(graph_driver, domain_spec)
    assert value == 0.0
