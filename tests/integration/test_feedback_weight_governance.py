"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, feedback]
owner: engine-team
status: active
--- /L9_META ---

Integration — feedback weight governance against Neo4j (CEG-462).

Runs the real ConvergenceLoop on the per-domain ``plasticos`` database
(enterprise testcontainer), with ``min_outcomes_for_recalculation`` lowered so
seeded outcomes trigger a recalculation:

- default: the recalculation is recorded as unapplied candidates; the stored
  weight, the weights scoring reads, and every boost_factor are unchanged, and
  no DimensionWeight is created
- the unapplied recalculation counts as the last one, so the next outcome does
  not recalculate again
- auto-apply: the weight moves and each write has one applied record carrying
  the prior weight
- concurrent applied recalculations of an existing DimensionWeight never record
  a stale prior, over repeated rounds (lost-update regression from e56f973); two
  concurrent first writes of a MISSING DimensionWeight can still each create one,
  since no (dimension, tenant) uniqueness constraint exists
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.config.settings import settings

pytestmark = pytest.mark.integration

_TENANT = "plasticos"
_SEEDED_DIM = "geo_proximity"
_SEEDED_WEIGHT = 0.5


def _spec(domain_spec: Any) -> Any:
    fl = domain_spec.feedbackloop
    sw = fl.signal_weights.model_copy(update={"min_outcomes_for_recalculation": 1})
    return domain_spec.model_copy(update={"feedbackloop": fl.model_copy(update={"signal_weights": sw})})


async def _clear(graph_driver: Any) -> None:
    await graph_driver.execute_query(
        "MATCH (n) WHERE (n:DimensionWeight OR n:DimensionWeightChange OR n:TransactionOutcome) "
        "AND n.tenant = $tenant DETACH DELETE n",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )


@pytest.fixture
async def governance_graph(graph_driver, plasticos_database, monkeypatch):
    monkeypatch.setattr(settings, "feedback_enabled", True)
    await _clear(graph_driver)
    # Ten outcomes, 6 wins; geo_proximity scored on the first 5 (4 wins).
    await graph_driver.execute_query(
        """
        UNWIND range(1, 10) AS i
        CREATE (o:TransactionOutcome {
            outcome_id: 'ceg462-' + toString(i), candidate_id: 'ceg462-c', tenant: $tenant,
            outcome: CASE WHEN i IN [1, 2, 3, 4, 7, 8] THEN 'success' ELSE 'failure' END,
            created_at: datetime()
        })
        FOREACH (_ IN CASE WHEN i <= 5 THEN [1] ELSE [] END | SET o.geo_proximity_score = 0.9)
        WITH DISTINCT 1 AS one
        CREATE (:DimensionWeight {dimension: $dim, tenant: $tenant, weight: $weight, boost_factor: 1.0,
                                  updated_at: datetime() - duration('P1D')})
        """,
        parameters={"tenant": _TENANT, "dim": _SEEDED_DIM, "weight": _SEEDED_WEIGHT},
        database=_TENANT,
    )
    yield graph_driver
    await _clear(graph_driver)


async def _weights(graph_driver: Any) -> list[dict[str, Any]]:
    return await graph_driver.execute_query(
        "MATCH (dw:DimensionWeight {tenant: $tenant}) "
        "RETURN dw.dimension AS dimension, dw.weight AS weight, dw.boost_factor AS boost ORDER BY dimension",
        parameters={"tenant": _TENANT},
        database=_TENANT,
    )


async def _records(graph_driver: Any, dimension: str | None = None) -> list[dict[str, Any]]:
    return await graph_driver.execute_query(
        "MATCH (c:DimensionWeightChange {tenant: $tenant}) WHERE $dim IS NULL OR c.dimension = $dim "
        "RETURN c.dimension AS dimension, c.prior_weight AS prior, c.new_weight AS new, c.applied AS applied, "
        "c.outcome_count AS outcomes, c.win_count AS wins, c.sample_size AS sample_size, "
        "c.recalculation_id AS recalculation_id, c.method AS method ORDER BY c.recorded_at, dimension",
        parameters={"tenant": _TENANT, "dim": dimension},
        database=_TENANT,
    )


async def _outcome(loop: Any) -> dict[str, Any]:
    result: dict[str, Any] = await loop.on_outcome_recorded(
        {"outcome_id": "ceg462-1", "candidate_id": "ceg462-c", "outcome": "success"}
    )
    return result


async def test_default_records_candidates_and_moves_no_weight(governance_graph, domain_spec):
    from engine.feedback.convergence import ConvergenceLoop
    from engine.scoring.assembler import ScoringAssembler

    spec = _spec(domain_spec)
    before = await ScoringAssembler(spec, graph_driver=governance_graph).load_learned_weights()

    metadata = await _outcome(ConvergenceLoop(governance_graph, spec))

    assert metadata["status"] == "complete"
    assert metadata["weights_recalculated"] is True
    assert metadata["weights_applied"] is False
    assert metadata["propagation"] == {"propagated": False, "reason": "boost_propagation_disabled"}
    # Nothing scoring reads moved, no DimensionWeight was created, no boost written.
    assert await _weights(governance_graph) == [
        {"dimension": _SEEDED_DIM, "weight": pytest.approx(_SEEDED_WEIGHT), "boost": pytest.approx(1.0)}
    ]
    after = await ScoringAssembler(spec, graph_driver=governance_graph).load_learned_weights()
    assert after == before == {_SEEDED_DIM: pytest.approx(_SEEDED_WEIGHT)}

    records = await _records(governance_graph)
    assert len(records) == len(spec.scoring.dimensions)
    assert all(r["applied"] is False and r["outcomes"] == 10 and r["wins"] == 6 for r in records)
    assert len({r["recalculation_id"] for r in records}) == 1
    seeded = next(r for r in records if r["dimension"] == _SEEDED_DIM)
    assert seeded["prior"] == pytest.approx(_SEEDED_WEIGHT)
    assert seeded["sample_size"] == 5
    assert seeded["new"] == pytest.approx(metadata["new_weights"][_SEEDED_DIM])
    # A dimension with no stored weight records an absent prior, not a number.
    assert all(r["prior"] is None for r in records if r["dimension"] != _SEEDED_DIM)


async def test_unapplied_recalculation_is_not_repeated_by_the_next_outcome(governance_graph, domain_spec):
    from engine.feedback.convergence import ConvergenceLoop

    loop = ConvergenceLoop(governance_graph, _spec(domain_spec))
    first = await _outcome(loop)
    second = await _outcome(loop)  # a replay: no new outcome node since the recalculation

    assert first["weights_recalculated"] is True
    assert second["weights_recalculated"] is False
    assert len(await _records(governance_graph)) == len(domain_spec.scoring.dimensions)


async def test_auto_apply_writes_the_weight_and_records_its_prior(governance_graph, domain_spec, monkeypatch):
    from engine.feedback.convergence import ConvergenceLoop
    from engine.scoring.assembler import ScoringAssembler

    monkeypatch.setattr(settings, "learned_weight_auto_apply_enabled", True)
    spec = _spec(domain_spec)

    metadata = await _outcome(ConvergenceLoop(governance_graph, spec))

    assert metadata["weights_applied"] is True
    new_weight = metadata["new_weights"][_SEEDED_DIM]
    loaded = await ScoringAssembler(spec, graph_driver=governance_graph).load_learned_weights()
    assert loaded[_SEEDED_DIM] == pytest.approx(new_weight)
    assert len(loaded) == len(spec.scoring.dimensions)
    (record,) = await _records(governance_graph, _SEEDED_DIM)
    assert record["applied"] is True
    # The legacy per-dimension copy is still written (now from a parameter map).
    (row,) = await governance_graph.execute_query(
        "MATCH (dw:DimensionWeight {tenant: $tenant, dimension: $dim}) RETURN dw.geo_proximity_weight AS copy",
        parameters={"tenant": _TENANT, "dim": _SEEDED_DIM},
        database=_TENANT,
    )
    assert row["copy"] == pytest.approx(new_weight)
    assert record["prior"] == pytest.approx(_SEEDED_WEIGHT)
    assert record["new"] == pytest.approx(new_weight)


_CONCURRENT_ROUNDS = 10


async def test_concurrent_applied_recalculations_never_record_a_stale_prior(governance_graph, domain_spec, monkeypatch):
    """Lost-update regression (CI run 43130108743, commit e56f973).

    A single-statement lock let the planner cache dw.weight before the MERGE, so
    two concurrent applied writes both recorded the seeded 0.5 as their prior; a
    local reproduction lost the update in 16 of 20 natural concurrent runs. The
    lock now runs as its own statement before the read, in one transaction.
    Repeated rounds make a regression all but certain to fail here.
    """
    from engine.feedback.signal_weights import SignalWeightCalculator

    monkeypatch.setattr(settings, "learned_weight_auto_apply_enabled", True)
    spec = _spec(domain_spec)

    for round_no in range(_CONCURRENT_ROUNDS):
        await governance_graph.execute_query(
            "MATCH (n) WHERE (n:DimensionWeight OR n:DimensionWeightChange) AND n.tenant = $tenant DETACH DELETE n",
            parameters={"tenant": _TENANT},
            database=_TENANT,
        )
        await governance_graph.execute_query(
            "CREATE (:DimensionWeight {dimension: $dim, tenant: $tenant, weight: $weight})",
            parameters={"tenant": _TENANT, "dim": _SEEDED_DIM, "weight": _SEEDED_WEIGHT},
            database=_TENANT,
        )
        await asyncio.gather(
            SignalWeightCalculator(governance_graph, spec).recalculate_weights(),
            SignalWeightCalculator(governance_graph, spec).recalculate_weights(),
        )
        records = await _records(governance_graph, _SEEDED_DIM)
        assert len(records) == 2, round_no
        new_weight = records[0]["new"]
        # The second writer saw the first one's committed weight, never the same stale prior.
        assert sorted(r["prior"] for r in records) == sorted([_SEEDED_WEIGHT, new_weight]), round_no
