"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, handlers, match, gates, traversal]
owner: engine-team
status: active
--- /L9_META ---

RU-011 / CEG-COR-033 (RG-014): a gate-failing candidate is excluded even when
the final traversal step is ``required: false``.

Runs on the synthetic orbital-logistics pack (ADR-112 Stage 4, A16) with a
trailing optional SERVES_ORBIT step, and both depots serve the orbit. Without
the ``WITH *`` row boundary the pack's port gate binds to the OPTIONAL MATCH
and the failing depot is returned with a null orbit.
"""

from __future__ import annotations

import pytest

from engine.config.schema import TraversalStepSpec
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, depot, serve

pytestmark = pytest.mark.integration

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:OrbitalDepot {depot_id: row.depot_id})
SET n += row
WITH n
MERGE (o:OrbitClass {class_code: 'LEO'})
MERGE (n)-[:SERVES_ORBIT]->(o)
"""


def _optional_last_step(spec):
    from engine.config.schema import TraversalSpec

    step = TraversalStepSpec(
        name="orbit_service",
        pattern="(candidate:OrbitalDepot)-[:SERVES_ORBIT]->(orbit:OrbitClass)",
        required=False,
        matchdirections=[DIRECTION],
    )
    return spec.model_copy(update={"traversal": TraversalSpec(steps=[step])})


@pytest.mark.asyncio
async def test_gate_failing_candidate_excluded_with_optional_last_step(
    engine_deps, graph_driver, domain_spec, clean_db
):
    from engine.handlers import handle_match

    serve(graph_driver, _optional_last_step(domain_spec))

    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"batch": [depot("d_pass", docking_ports=6), depot("d_fail", docking_ports=1)]},
        database=ORBITAL,
    )

    result = await handle_match(
        ORBITAL,
        {"query": {"min_ports": 4}, "match_direction": DIRECTION, "top_n": 10},
    )
    ids = {c["candidate"].get("depot_id") for c in result["candidates"]}
    assert "d_fail" not in ids
    assert "d_pass" in ids
