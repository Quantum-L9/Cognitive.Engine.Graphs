"""Integration test — causal explanation depth (RU-074 / CEG-COR-121, RG-097).

`CausalSubgraphSerializer.serialize_neighborhood` matched a one-hop pattern
(`[r:T]`), so `depth_reached` was never above 1. Seeds a 2-hop chain
F-1 -> F-2 -> outcome in the plasticos database and verifies that
`max_depth=2` reaches depth 2 (and that `max_depth=1` still stops at 1).

Requires a live Neo4j; skips automatically when testcontainers / NEO4J_TEST_URI
is unavailable (see tests/conftest.py::neo4j_container).
"""

from __future__ import annotations

import pytest

from engine.causal.serializer import CausalSubgraphSerializer

SEED_CYPHER = """
MERGE (a:Facility {facility_id: 'F-1', entity_id: 'F-1'})
MERGE (b:Facility {facility_id: 'F-2', entity_id: 'F-2'})
MERGE (o:TransactionOutcome {outcome_id: 'ot-depth', entity_id: 'ot-depth'})
MERGE (a)-[r1:RESULTED_IN]->(b)
SET r1.confidence = 0.9
MERGE (b)-[r2:RESULTED_IN]->(o)
SET r2.confidence = 0.8
"""


@pytest.mark.asyncio
async def test_two_hop_chain_reaches_depth_two(engine_deps, graph_driver, domain_loader, clean_db):
    spec = domain_loader.load_domain("plasticos")
    await graph_driver.execute_write(cypher=SEED_CYPHER, parameters={}, database=spec.domain.id)
    serializer = CausalSubgraphSerializer(graph_driver, spec)

    deep = await serializer.serialize_neighborhood("F-1", "Facility", max_depth=2, id_property="facility_id")
    assert deep["depth_reached"] == 2
    assert deep["nodes_visited"] == 3

    shallow = await serializer.serialize_neighborhood("F-1", "Facility", max_depth=1, id_property="facility_id")
    assert shallow["depth_reached"] == 1
