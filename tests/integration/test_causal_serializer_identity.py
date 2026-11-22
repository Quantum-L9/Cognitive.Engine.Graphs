"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [causal, serializer, identity, integration]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 N5 (F09; cases C07, C08, C09, C11, C12, C26).

``CausalSubgraphSerializer.serialize_neighborhood`` named every path node by
the START node's id property. A path crossing labels with different declared
ids (``OrbitalDepot.depot_id`` -> ``DockingResult.outcome_id``) read the other
labels as NULL, so distinct outcome nodes collapsed into one identity and
``nodes_visited`` under-counted. These tests seed a real Neo4j (synthetic
orbital-logistics tenant, ADR-112) and read the serializer's result back.
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from engine.causal.serializer import CausalSubgraphSerializer
from engine.config.schema import DomainSpec

pytestmark = [pytest.mark.integration, pytest.mark.finding("F09")]

_EDGES = [
    {"edge_type": "RESULTED_IN", "source_label": "OrbitalDepot", "target_label": "DockingResult"},
    {"edge_type": "CONTRIBUTED_TO", "source_label": "OrbitalDepot", "target_label": "OrbitalDepot"},
]


def _causal_spec(base: DomainSpec, *, database: str | None = None) -> DomainSpec:
    raw: dict[str, Any] = base.model_dump(by_alias=True)
    raw["causal"] = {"enabled": True, "causal_edges": _EDGES, "chain_depth_limit": 3}
    if database is not None:
        raw["domain"]["id"] = database
    return DomainSpec.model_validate(raw)


# Two outcomes of D-1 (no entity_id: the declared outcome_id is their only id),
# one reached through a CONTRIBUTED_TO hop, plus an undeclared edge type.
SEED = """
MERGE (a:OrbitalDepot {depot_id: 'D-1', name: 'Depot One'})
MERGE (b:OrbitalDepot {depot_id: 'D-2', name: 'Depot Two'})
MERGE (o1:DockingResult {outcome_id: 'out-1'})
MERGE (o2:DockingResult {outcome_id: 'out-2'})
MERGE (o3:DockingResult {outcome_id: 'out-3'})
MERGE (hidden:OrbitalDepot {depot_id: 'D-X'})
MERGE (a)-[:RESULTED_IN {confidence: 0.9}]->(o1)
MERGE (a)-[:CONTRIBUTED_TO {confidence: 0.7}]->(b)
MERGE (b)-[:RESULTED_IN {confidence: 0.8}]->(o2)
MERGE (b)-[:CONTRIBUTED_TO {confidence: 0.6}]->(c:OrbitalDepot {depot_id: 'D-3'})
MERGE (c)-[:RESULTED_IN {confidence: 0.5}]->(o3)
MERGE (a)-[:UNDECLARED_LINK]->(hidden)
"""


@pytest.mark.asyncio
async def test_mixed_label_path_nodes_keep_their_declared_identity(
    engine_deps, graph_driver, domain_loader, clean_db
) -> None:
    """C07 / C11: every node on a mixed-label path carries its own declared id."""
    spec = _causal_spec(domain_loader.load_domain("orbital-logistics"))
    await graph_driver.execute_write(cypher=SEED, parameters={}, database=spec.domain.id)
    serializer = CausalSubgraphSerializer(graph_driver, spec)

    result = await serializer.serialize_neighborhood("D-1", "OrbitalDepot", max_depth=2, id_property="depot_id")

    # D-1, out-1, D-2, out-2, D-3 — five distinct identities within two hops.
    assert result["nodes_visited"] == 5, result
    assert "DockingResult[out-1]" in result["serialized"]
    assert "DockingResult[out-2]" in result["serialized"]
    assert "OrbitalDepot[Depot One]" in result["serialized"]


@pytest.mark.asyncio
async def test_depth_bound_and_edge_whitelist(engine_deps, graph_driver, domain_loader, clean_db) -> None:
    """C08: BFS stops at the approved bound and follows only declared edge types."""
    spec = _causal_spec(domain_loader.load_domain("orbital-logistics"))
    await graph_driver.execute_write(cypher=SEED, parameters={}, database=spec.domain.id)
    serializer = CausalSubgraphSerializer(graph_driver, spec)

    deep = await serializer.serialize_neighborhood("D-1", "OrbitalDepot", max_depth=10, id_property="depot_id")
    assert deep["depth_reached"] == 3  # clamped to chain_depth_limit=3
    assert "out-3" in deep["serialized"]
    assert "D-X" not in deep["serialized"]
    assert "UNDECLARED_LINK" not in deep["edges_traversed"]
    assert set(deep["edges_traversed"]) <= {"RESULTED_IN", "CONTRIBUTED_TO"}

    one = await serializer.serialize_neighborhood("D-1", "OrbitalDepot", max_depth=1, id_property="depot_id")
    assert one["depth_reached"] == 1
    assert "out-2" not in one["serialized"]


@pytest.mark.asyncio
async def test_query_failure_is_distinguishable_from_empty(engine_deps, graph_driver, domain_loader, clean_db) -> None:
    """C09: a query error logs causal_serialization_failed with its class; empty logs nothing."""
    spec = _causal_spec(domain_loader.load_domain("orbital-logistics"))
    serializer = CausalSubgraphSerializer(graph_driver, spec)

    with capture_logs() as empty_logs:
        empty = await serializer.serialize_neighborhood("NOPE", "OrbitalDepot", id_property="depot_id")
    assert empty["nodes_visited"] == 0
    assert not [e for e in empty_logs if e["event"] == "causal_serialization_failed"]

    missing_db = _causal_spec(domain_loader.load_domain("orbital-logistics"), database="no-such-tenant-db")
    broken = CausalSubgraphSerializer(graph_driver, missing_db)
    with capture_logs() as failed_logs:
        failed = await broken.serialize_neighborhood("D-1", "OrbitalDepot", id_property="depot_id")
    assert failed["serialized"] == ""  # no fabricated explanation
    events = [e for e in failed_logs if e["event"] == "causal_serialization_failed"]
    assert len(events) == 1
    assert events[0]["error_type"]


@pytest.mark.asyncio
async def test_tenant_databases_do_not_cross(engine_deps, graph_driver, domain_loader, clean_db) -> None:
    """C12 / C26: same node-id strings in two tenant databases stay in their own database."""
    other_db = "orbital-logistics-shadow"
    await graph_driver.execute_query(
        cypher=f"CREATE DATABASE `{other_db}` IF NOT EXISTS WAIT", parameters={}, database="system"
    )
    spec = _causal_spec(domain_loader.load_domain("orbital-logistics"))
    shadow = _causal_spec(domain_loader.load_domain("orbital-logistics"), database=other_db)
    await graph_driver.execute_write(cypher=SEED, parameters={}, database=spec.domain.id)
    try:
        await graph_driver.execute_write(
            cypher=(
                "MERGE (a:OrbitalDepot {depot_id: 'D-1'}) "
                "MERGE (o:DockingResult {outcome_id: 'shadow-out'}) "
                "MERGE (a)-[:RESULTED_IN {confidence: 0.9}]->(o)"
            ),
            parameters={},
            database=other_db,
        )
        mine = await CausalSubgraphSerializer(graph_driver, spec).serialize_neighborhood(
            "D-1", "OrbitalDepot", max_depth=1, id_property="depot_id"
        )
        theirs = await CausalSubgraphSerializer(graph_driver, shadow).serialize_neighborhood(
            "D-1", "OrbitalDepot", max_depth=1, id_property="depot_id"
        )
    finally:
        await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=other_db)

    assert "shadow-out" not in mine["serialized"]
    assert "out-1" in mine["serialized"]
    assert theirs["serialized"] == "OrbitalDepot[D-1] -[RESULTED_IN]-> DockingResult[shadow-out]"
    assert theirs["nodes_visited"] == 2
