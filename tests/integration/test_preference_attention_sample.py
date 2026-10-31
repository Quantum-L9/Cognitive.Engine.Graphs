"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, hgkr]
owner: engine-team
status: active
--- /L9_META ---

Integration — preference_attention samples once (RU-021 / CEG-COR-082, RG-027).

The ratio is (#community matches within the K most recent successes) / (size of
that sample). The former expression sliced the filtered and unfiltered lists
separately, so 100 successes with 50 in the candidate's community and K=28
scored min(28, 50) / min(28, 100) = 1.0. The compiled expression is evaluated
against real nodes in the default ``neo4j`` database.

preference_attention stays withheld at pack admission (R1b-DEC-01 / DEC-08 "A",
RU-020): the dimension is built directly here, as no served pack can declare
its ``metadata``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.config.schema import ComputationType, ScoringDimensionSpec

_Q = "_PrefAttnQuery"
_C = "_PrefAttnCandidate"


def _dimension() -> MagicMock:
    dim = MagicMock(spec=ScoringDimensionSpec)
    dim.name = "pref"
    dim.computation = ComputationType.PREFERENCEATTENTION
    dim.candidateprop = "community_id"
    dim.defaultwhennull = 0.0
    dim.metadata = {"success_value": "success", "outcome_node": "_PrefAttnOutcome"}
    return dim


async def _score(graph_driver, domain_spec, outcomes: list[tuple[int, str, int]], k: int) -> float:
    from engine.scoring.assembler import ScoringAssembler

    spec = domain_spec.model_copy(
        update={"scoring": domain_spec.scoring.model_copy(update={"preference_sample_size": k})}
    )
    assembler = ScoringAssembler(spec)
    expr = assembler._compile_preference_attention(_dimension())
    edge = spec.feedbackloop.outcome_edge_type
    await graph_driver.execute_query(
        f"CREATE (:{_Q} {{qid: 'q'}}), (:{_C} {{cid: 'c', community_id: 7}})", parameters={}, database="neo4j"
    )
    await graph_driver.execute_query(
        f"MATCH (qe:{_Q} {{qid: 'q'}}) UNWIND $rows AS row "
        f"CREATE (qe)-[:{edge}]->(:_PrefAttnOutcome {{outcome_type: row.kind, community_id: row.community, "
        "created_at: datetime({epochSeconds: row.age})})",
        parameters={"rows": [{"age": a, "kind": kind, "community": c} for a, kind, c in outcomes]},
        database="neo4j",
    )
    try:
        rows = await graph_driver.execute_query(
            f"MATCH (qe:{_Q} {{qid: 'q'}}), (candidate:{_C} {{cid: 'c'}}) RETURN {expr} AS pref",
            parameters=dict(assembler._query_params),
            database="neo4j",
        )
    finally:
        await graph_driver.execute_query(
            f"MATCH (n) WHERE n:{_Q} OR n:{_C} OR n:_PrefAttnOutcome DETACH DELETE n",
            parameters={},
            database="neo4j",
        )
    return float(rows[0]["pref"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hundred_successes_fifty_matching_k28_scores_half(graph_driver, domain_spec):
    outcomes = [(1_700_000_000 + i, "success", 7 if i % 2 == 0 else 9) for i in range(100)]
    assert await _score(graph_driver, domain_spec, outcomes, k=28) == pytest.approx(0.5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_is_the_most_recent_successes(graph_driver, domain_spec):
    old_matching = [(1_600_000_000 + i, "success", 7) for i in range(50)]
    recent_other = [(1_700_000_000 + i, "success", 9) for i in range(10)]
    failures = [(1_800_000_000 + i, "failure", 7) for i in range(10)]
    assert await _score(graph_driver, domain_spec, old_matching + recent_other + failures, k=10) == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_successes_returns_default(graph_driver, domain_spec):
    failures = [(1_700_000_000 + i, "failure", 7) for i in range(5)]
    assert await _score(graph_driver, domain_spec, failures, k=28) == 0.0
