"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, resolution, handlers, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

Integration test -- the ``resolve`` action runs on the tenant's admitted spec
(CEG-NATIVE-CLOSURE-001 N7a-resolve, follow-up to #539).

``handle_resolve`` admits the tenant's DomainSpec once. Entity resolution must
use that spec's declared id (``depot_id``) and outcome label, and must not
re-admit the domain through the engine state mid-request: a pack republished
between the two loads would otherwise resolve one request on two revisions.

Requires a live Neo4j (tests/conftest.py::neo4j_container).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import DomainSpec, SemanticRegistrySpec

pytestmark = pytest.mark.integration

ORBITAL = "orbital-logistics"
LABEL = "OrbitalDepot"


def _resolving(spec: DomainSpec) -> DomainSpec:
    registry = SemanticRegistrySpec(
        enabled=True,
        entity_labels=[LABEL],
        similarity_threshold=0.9,
        property_weight=1.0,
        structural_weight=0.0,
        behavioral_weight=0.0,
        comparison_properties=["docking_ports"],
    )
    return spec.model_copy(update={"semantic_registry": registry})


@pytest.mark.asyncio
async def test_resolve_action_uses_the_admitted_spec_once(graph_driver, domain_loader, domain_database) -> None:
    """A batch resolve on depot_id-keyed depots links on depot_id after one admission."""
    from engine.handlers import handle_resolve, init_dependencies
    from engine.state import get_state

    admitted = _resolving(domain_loader.load_domain(ORBITAL))
    loader = MagicMock()
    loader.load_domain.return_value = admitted
    get_state().reset()
    init_dependencies(graph_driver, loader)
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=domain_database)
    await graph_driver.execute_write(
        cypher="""
        CREATE (:OrbitalDepot {depot_id: 'd1', docking_ports: 6})-[:LINKS]->(:Thing {thing_id: 1})
        CREATE (:OrbitalDepot {depot_id: 'd2', docking_ports: 6})
        CREATE (:OrbitalDepot {depot_id: 'd3', docking_ports: 2})
        """,
        parameters={},
        database=domain_database,
    )
    try:
        result: dict[str, Any] = await handle_resolve(ORBITAL, {"entity_type": LABEL, "mode": "batch"})
        rows = await graph_driver.execute_query(
            cypher="MATCH (a:OrbitalDepot)-[:RESOLVED_FROM]->(b) RETURN a.depot_id AS s, b.depot_id AS t",
            parameters={},
            database=domain_database,
        )
    finally:
        await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=domain_database)
        get_state().reset()

    assert result["status"] == "batch_resolved"
    assert result["total_merged"] == 1
    assert [(r["s"], r["t"]) for r in rows] == [("d2", "d1")]
    # One admission for the whole request: the resolver must not re-load the domain.
    assert loader.load_domain.call_count == 1
