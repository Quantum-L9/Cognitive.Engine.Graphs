"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, resolution, entity-resolver]
owner: engine-team
status: active
--- /L9_META ---

Integration test -- entity resolution scores every member before writing (RU-101
follow-up, PR #367 review).

With structural similarity weighted, a RESOLVED_FROM edge changes the
canonical's neighbor set. The subject s is linked to the canonical c first; m
must still be judged on c's neighbors as they were before any merge:

  * c has neighbors {n1..n4}, s and m have {n1, n2, n3};
  * score = 0.5 * property (1.0) + 0.5 * neighbor Jaccard;
  * before any write J(m, c) = 3/4 -> 0.875 >= 0.85;
  * after s -> c, c's neighbors gain s, J(m, c) = 3/5 -> 0.8 < 0.85.

Requires a live Neo4j; skips automatically when testcontainers / NEO4J_TEST_URI
is unavailable (see tests/conftest.py::neo4j_container).
"""

from __future__ import annotations

import pytest

from engine.config.schema import SemanticRegistrySpec
from engine.resolution.resolver import EntityResolver

pytestmark = pytest.mark.integration

DB = "neo4j"  # wiped by clean_db

SEED_CYPHER = """
CREATE (s:Supplier {entity_id: 's', name: 'Acme'})
CREATE (c:Supplier {entity_id: 'c', name: 'Acme'})
CREATE (m:Supplier {entity_id: 'm', name: 'Acme'})
CREATE (n1:Thing {thing_id: 1}), (n2:Thing {thing_id: 2}), (n3:Thing {thing_id: 3}), (n4:Thing {thing_id: 4})
CREATE (c)-[:LINKS]->(n1), (c)-[:LINKS]->(n2), (c)-[:LINKS]->(n3), (c)-[:LINKS]->(n4)
CREATE (s)-[:LINKS]->(n1), (s)-[:LINKS]->(n2), (s)-[:LINKS]->(n3)
CREATE (m)-[:LINKS]->(n1), (m)-[:LINKS]->(n2), (m)-[:LINKS]->(n3)
"""


@pytest.mark.asyncio
async def test_members_are_scored_before_resolution_edges_change_the_graph(graph_driver, clean_db) -> None:
    from engine.config.settings import settings

    assert settings.entity_resolution_merge_integrity is True
    await graph_driver.execute_write(cypher=SEED_CYPHER, parameters={}, database=DB)
    spec = SemanticRegistrySpec(
        enabled=True,
        entity_labels=["Supplier"],
        similarity_threshold=0.85,
        property_weight=0.5,
        structural_weight=0.5,
        behavioral_weight=0.0,
        comparison_properties=["name"],
    )
    resolver = EntityResolver(spec, graph_driver, DB)

    result = await resolver.resolve_entity("s", "Supplier")

    rows = await graph_driver.execute_query(
        cypher="MATCH (a:Supplier)-[r:RESOLVED_FROM]->(b:Supplier) RETURN a.entity_id AS src, b.entity_id AS dst",
        parameters={},
        database=DB,
    )
    assert result["canonical_id"] == "c"
    assert sorted((row["src"], row["dst"]) for row in rows) == [("m", "c"), ("s", "c")]
    assert sorted(result["merged_entity_ids"]) == ["m", "s"]
    assert result["merged_count"] == 2
