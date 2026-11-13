"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, gds, cooccurrence]
owner: engine-team
status: active
--- /L9_META ---

Integration test — GDS co-occurrence power-law pruning (RU-098 / CEG-COR-168).

Runs the ``cooccurrence`` job against a live Neo4j (testcontainers, as
test_causal_temporal_decay.py) on two fixtures:

  * K15 — fifteen nodes whose every pair co-occurs, each pair with a distinct
    weight. Every node has degree 14 > 10, so keep_count = floor(14^0.86) = 9:
    after pruning every node retains >= 9 edges, and a second run yields the
    identical edge set;
  * a star — one hub co-occurring with 20 leaves that do not co-occur with each
    other. Leaves have degree 1 (below the > 10 floor), so every leaf keeps its
    edge although the hub prunes.

The unordered legacy prune ended K15 nodes with 1-5 edges and stripped leaves
(emulated in the planner's cooccurrence_prune_emulation.py).

Requires a live Neo4j; skips automatically when testcontainers / NEO4J_TEST_URI
is unavailable (see tests/conftest.py::neo4j_container).
"""

from __future__ import annotations

import itertools

import pytest

from engine.config.schema import DomainSpec, GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.gds.scheduler import GDSScheduler

pytestmark = pytest.mark.integration

DB = "orbital-logistics"  # synthetic tenant db (ADR-112 Stage 4, A16): domain_database creates it, clean_db wipes it

# Each pair (a, b) shares `k` private Resource nodes, so Step 1 writes
# CO_USES with weight k between them.
SEED_PAIRS_CYPHER = """
UNWIND $sids AS sid
MERGE (:Site {sid: sid})
WITH count(*) AS seeded_sites
UNWIND $pairs AS p
MATCH (a:Site {sid: p.a}), (b:Site {sid: p.b})
UNWIND range(1, p.k) AS _x
CREATE (c:Resource)
CREATE (a)-[:USES]->(c), (b)-[:USES]->(c)
"""


def _spec() -> DomainSpec:
    job = GDSJobSpec(
        name="co_usage",
        algorithm="cooccurrence",
        schedule=GDSJobScheduleSpec(type="manual"),
        projection=GDSProjectionSpec(nodelabels=["Site"], edgetypes=["USES"]),
        sourceedge="USES",
        writeedge="CO_USES",
    )
    raw = {
        "domain": {"id": DB, "name": "Cooccurrence", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {
                    "label": "Site",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "sid", "type": "string", "required": True}],
                },
                {
                    "label": "Resource",
                    "managedby": "sync",
                    "properties": [],
                },
                {
                    "label": "Query",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "query_id", "type": "int", "required": True}],
                },
            ],
            "edges": [
                {
                    "type": "USES",
                    "from": "Site",
                    "to": "Resource",
                    "direction": "DIRECTED",
                    "category": "market",
                    "managedby": "sync",
                },
            ],
        },
        "matchentities": {
            "candidate": [{"label": "Site", "matchdirection": "d1"}],
            "queryentity": [{"label": "Query", "matchdirection": "d1"}],
        },
        "queryschema": {"matchdirections": ["d1"], "fields": []},
        "traversal": {"steps": []},
        "gates": [],
        "scoring": {"dimensions": []},
        "gdsjobs": [job.model_dump()],
    }
    return DomainSpec(**raw)


async def _seed(graph_driver, pairs: list[dict]) -> None:
    sids = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    await graph_driver.execute_write(
        cypher=SEED_PAIRS_CYPHER,
        parameters={"sids": sids, "pairs": pairs},
        database=DB,
    )


async def _edges(graph_driver) -> set[frozenset[str]]:
    rows = await graph_driver.execute_query(
        "MATCH (a:Site)-[:CO_USES]->(b:Site) RETURN a.sid AS a, b.sid AS b",
        parameters={},
        database=DB,
    )
    return {frozenset((row["a"], row["b"])) for row in rows}


def _degrees(edges: set[frozenset[str]]) -> dict[str, int]:
    degree: dict[str, int] = {}
    for edge in edges:
        for node in edge:
            degree[node] = degree.get(node, 0) + 1
    return degree


@pytest.mark.asyncio
async def test_k15_keeps_keep_count_per_node_and_is_deterministic(graph_driver, domain_database, clean_db) -> None:
    from engine.config.settings import settings

    assert settings.gds_cooccurrence_deterministic_prune is True
    nodes = [f"k{i:02d}" for i in range(15)]
    pairs = [{"a": a, "b": b, "k": 2 + rank} for rank, (a, b) in enumerate(itertools.combinations(nodes, 2))]
    await _seed(graph_driver, pairs)
    spec = _spec()
    scheduler = GDSScheduler(spec, graph_driver)

    first = await scheduler.execute_job(spec.gdsjobs[0])
    assert first["status"] == "success"
    after_first = await _edges(graph_driver)
    degree = _degrees(after_first)
    assert set(degree) == set(nodes)
    assert min(degree.values()) >= int(14**0.86) == 9
    assert first["edges_pruned"] == 105 - len(after_first)

    second = await scheduler.execute_job(spec.gdsjobs[0])
    assert second["status"] == "success"
    assert await _edges(graph_driver) == after_first


@pytest.mark.asyncio
async def test_star_leaves_below_the_floor_keep_their_edge(graph_driver, domain_database, clean_db) -> None:
    pairs = [{"a": "hub", "b": f"leaf{i:02d}", "k": 2} for i in range(20)]
    await _seed(graph_driver, pairs)
    spec = _spec()
    result = await GDSScheduler(spec, graph_driver).execute_job(spec.gdsjobs[0])
    assert result["status"] == "success"
    edges = await _edges(graph_driver)
    degree = _degrees(edges)
    assert all(degree.get(f"leaf{i:02d}", 0) == 1 for i in range(20))
    assert degree["hub"] == 20
    assert result["edges_pruned"] == 0
