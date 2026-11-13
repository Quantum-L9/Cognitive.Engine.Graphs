"""Integration — ScorePropagator applies the factor once per event (RU-028 / CEG-COR-117, RG-036).

A candidate with three prior outcomes and two tenant DimensionWeight nodes:
the legacy query produced 3 x 2 rows, multiplying each boost_factor by
1.15^3 and reporting 6 affected. One event now multiplies each boost_factor
exactly once (1.15) and reports 2 affected; the result is clamped to the
spec's signal_weights [min_weight, max_weight].

Runs against the per-domain ``orbital-logistics`` database (synthetic pack,
ADR-112 Stage 4 / A16; enterprise testcontainer). Outcomes carry the pack's
declared outcome label, DockingResult.
"""

from __future__ import annotations

import pytest

_TENANT = "orbital-logistics"
_CANDIDATE = "RU028-D-1"


async def _seed(graph_driver, boost_factor: float = 1.0) -> None:
    await graph_driver.execute_query(
        """
        UNWIND range(1, 3) AS i
        CREATE (:DockingResult {candidate_id: $candidate_id, tenant: $tenant, outcome: 'success', seq: i})
        WITH DISTINCT 1 AS one
        UNWIND ['ru028_dim_a', 'ru028_dim_b'] AS dim
        CREATE (:DimensionWeight {dimension: dim, tenant: $tenant, weight: 1.0, boost_factor: $boost})
        """,
        parameters={"candidate_id": _CANDIDATE, "tenant": _TENANT, "boost": boost_factor},
        database=_TENANT,
    )


async def _boosts(graph_driver) -> list[float]:
    rows = await graph_driver.execute_query(
        "MATCH (dw:DimensionWeight {tenant: $tenant}) WHERE dw.dimension STARTS WITH 'ru028_' "
        "RETURN dw.boost_factor AS b ORDER BY dw.dimension",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )
    return [float(r["b"]) for r in rows]


async def _cleanup(graph_driver) -> None:
    await graph_driver.execute_query(
        "MATCH (o:DockingResult {candidate_id: $candidate_id, tenant: $tenant}) DETACH DELETE o",
        parameters={"candidate_id": _CANDIDATE, "tenant": _TENANT},
        database=_TENANT,
    )
    # Every tenant DimensionWeight is test data here; clearing them all makes
    # `candidates_affected` exactly the two seeded nodes.
    await graph_driver.execute_query(
        "MATCH (dw:DimensionWeight {tenant: $tenant}) DETACH DELETE dw",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_outcomes_apply_factor_once(graph_driver, domain_spec, domain_database):
    from engine.feedback.score_propagator import ScorePropagator

    await _cleanup(graph_driver)
    try:
        await _seed(graph_driver)
        result = await ScorePropagator(graph_driver, domain_spec).propagate_outcome(
            {"outcome": "success", "candidate_id": _CANDIDATE}, boost_factor=1.15
        )
        boosts = await _boosts(graph_driver)
    finally:
        await _cleanup(graph_driver)

    assert result["candidates_affected"] == 2
    assert boosts == [pytest.approx(1.15), pytest.approx(1.15)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_boost_is_clamped_to_max_weight(graph_driver, domain_spec, domain_database):
    from engine.feedback.score_propagator import ScorePropagator

    max_weight = domain_spec.feedbackloop.signal_weights.max_weight
    await _cleanup(graph_driver)
    try:
        await _seed(graph_driver, boost_factor=max_weight)
        await ScorePropagator(graph_driver, domain_spec).propagate_outcome(
            {"outcome": "success", "candidate_id": _CANDIDATE}, boost_factor=1.15
        )
        boosts = await _boosts(graph_driver)
    finally:
        await _cleanup(graph_driver)

    assert boosts == [pytest.approx(max_weight), pytest.approx(max_weight)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_outcome_no_update(graph_driver, domain_spec, domain_database):
    from engine.feedback.score_propagator import ScorePropagator

    await _cleanup(graph_driver)
    try:
        await _seed(graph_driver)
        result = await ScorePropagator(graph_driver, domain_spec).propagate_outcome(
            {"outcome": "success", "candidate_id": "RU028-NO-OUTCOMES"}, boost_factor=1.15
        )
        boosts = await _boosts(graph_driver)
    finally:
        await _cleanup(graph_driver)

    assert result["candidates_affected"] == 0
    assert boosts == [pytest.approx(1.0), pytest.approx(1.0)]
