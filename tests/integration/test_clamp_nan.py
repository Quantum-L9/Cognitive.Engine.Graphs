"""Integration — the W1-02 clamp maps NaN to 0.0 (RU-031 / CEG-COR-166, RG-040).

``lognormalized`` compiles to ``log(1 + coalesce(candidate.value, 0)) /
log(1 + max)``; for value -5 that is ``log(-4)``, which Cypher evaluates to
NaN. NaN compares false to both clamp bounds, so it used to fall through to
ELSE and poison the weighted score. The clamp now tests ``isNaN`` after
``IS NULL``. The test also proves ``isNaN`` accepts an INTEGER expression on
the pinned Neo4j image.
"""

from __future__ import annotations

import math

import pytest

from engine.config.schema import ComputationType, ScoringDimensionSpec, ScoringSource

_LABEL = "_ClampNaNProbe"


def _lognormalized() -> ScoringDimensionSpec:
    return ScoringDimensionSpec(
        name="volume",
        source=ScoringSource.CANDIDATEPROPERTY,
        computation=ComputationType.LOGNORMALIZED,
        candidateprop="value",
        maxvalue=1000.0,
        weightkey="w_volume",
        defaultweight=0.5,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lognormalized_negative_value_clamps_to_zero(graph_driver, domain_spec):
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(domain_spec)
    raw = assembler._compile_lognormalized(_lognormalized())
    clamped = assembler._clamp_expression(raw)
    await graph_driver.execute_query(
        f"CREATE (candidate:{_LABEL} {{value: $value}})", parameters={"value": -5}, database="neo4j"
    )
    try:
        rows = await graph_driver.execute_query(
            f"MATCH (candidate:{_LABEL}) "
            f"WITH candidate, {raw} AS unclamped, {clamped} AS volume "
            "RETURN unclamped, volume, 0.5 * volume AS score",
            parameters={},
            database="neo4j",
        )
    finally:
        await graph_driver.execute_query("MATCH (n:_ClampNaNProbe) DETACH DELETE n", parameters={}, database="neo4j")

    row = rows[0]
    assert math.isnan(float(row["unclamped"]))  # the producer really emits NaN
    assert float(row["volume"]) == 0.0
    assert math.isfinite(float(row["score"]))
    assert float(row["score"]) == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clamp_accepts_integer_expression(graph_driver, domain_spec):
    """isNaN() on an INTEGER operand is accepted and false, so integer dimensions clamp as before."""
    from engine.scoring.assembler import ScoringAssembler

    clamped = ScoringAssembler(domain_spec)._clamp_expression("$n")
    rows = await graph_driver.execute_query(
        f"RETURN {clamped} AS low, isNaN($n) AS nan", parameters={"n": 1}, database="neo4j"
    )
    assert rows[0]["nan"] is False
    assert float(rows[0]["low"]) == 1.0
