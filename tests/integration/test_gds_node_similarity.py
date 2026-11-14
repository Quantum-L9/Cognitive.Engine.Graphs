"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, gds, node-similarity]
owner: engine-team
status: active
--- /L9_META ---

Integration test — GDS ``nodeSimilarity`` job (CEG-GDS-SIM).

Runs the job against the live Neo4j enterprise testcontainer with the
graph-data-science plugin (tests/conftest.py::neo4j_container) on a synthetic
bipartite graph: Sites USE Resources.

  * s1 and s2 use exactly the same three resources  -> Jaccard 1.0
  * s3 shares one of its two resources with s1/s2    -> Jaccard 1/4
  * s4 uses a private resource                        -> no similarity

It proves the declared ``writeedge`` relationships are written with the
declared ``writeproperty``, that ``similaritycutoff`` bounds them, that a
second run replaces rather than duplicates, that the in-memory projection is
dropped, and that the flag off writes nothing.

Requires a live Neo4j; skips automatically when testcontainers / NEO4J_TEST_URI
is unavailable (fails under CEG_REQUIRE_INTEGRATION=1).
"""

from __future__ import annotations

import pytest

from engine.config.schema import (
    DomainSpec,
    GDSJobScheduleSpec,
    GDSJobSpec,
    GDSNodeSimilaritySpec,
    GDSProjectionSpec,
)
from engine.gds.scheduler import GDSScheduler

pytestmark = pytest.mark.integration

DB = "orbital-logistics"  # synthetic tenant db (ADR-112 Stage 4, A16): domain_database creates it, clean_db wipes it

SEED_CYPHER = """
UNWIND $uses AS u
MERGE (s:Site {sid: u.site})
MERGE (r:Resource {rid: u.resource})
MERGE (s)-[:USES]->(r)
"""

USES = [
    {"site": "s1", "resource": "r1"},
    {"site": "s1", "resource": "r2"},
    {"site": "s1", "resource": "r3"},
    {"site": "s2", "resource": "r1"},
    {"site": "s2", "resource": "r2"},
    {"site": "s2", "resource": "r3"},
    {"site": "s3", "resource": "r3"},
    {"site": "s3", "resource": "r4"},
    {"site": "s4", "resource": "r5"},
]


def _spec(cutoff: float | None = None) -> DomainSpec:
    job = GDSJobSpec(
        name="site_similarity",
        algorithm="nodeSimilarity",
        schedule=GDSJobScheduleSpec(type="manual"),
        projection=GDSProjectionSpec(nodelabels=["Site", "Resource"], edgetypes=["USES"]),
        writeedge="SIMILAR_SITE",
        writeproperty="similarity",
        nodesimilarity=GDSNodeSimilaritySpec(topk=5, similaritycutoff=cutoff),
    )
    raw = {
        "domain": {"id": DB, "name": "Node similarity", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {
                    "label": "Site",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "sid", "type": "string", "required": True}],
                },
                {"label": "Resource", "managedby": "sync", "properties": []},
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
                    "category": "capability",
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


async def _seed(graph_driver) -> None:
    await graph_driver.execute_write(cypher=SEED_CYPHER, parameters={"uses": USES}, database=DB)


async def _similar(graph_driver) -> dict[tuple[str, str], float]:
    rows = await graph_driver.execute_query(
        "MATCH (a:Site)-[r:SIMILAR_SITE]->(b:Site) RETURN a.sid AS a, b.sid AS b, r.similarity AS score",
        parameters={},
        database=DB,
    )
    return {(row["a"], row["b"]): row["score"] for row in rows}


async def _projections(graph_driver) -> list[str]:
    rows = await graph_driver.execute_query(
        "CALL gds.graph.list() YIELD graphName RETURN graphName", parameters={}, database=DB
    )
    return [row["graphName"] for row in rows]


@pytest.fixture
def flag_on(monkeypatch):
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "gds_node_similarity_enabled", True)


@pytest.mark.asyncio
async def test_writes_declared_similarity_edges(graph_driver, domain_database, clean_db, flag_on) -> None:
    await _seed(graph_driver)
    spec = _spec()
    result = await GDSScheduler(spec, graph_driver).execute_job(spec.gdsjobs[0])

    assert result["status"] == "success", result
    edges = await _similar(graph_driver)
    assert edges[("s1", "s2")] == pytest.approx(1.0)
    assert edges[("s2", "s1")] == pytest.approx(1.0)
    assert edges[("s3", "s1")] == pytest.approx(0.25)
    assert not any("s4" in pair for pair in edges)
    assert result["relationships_written"] == len(edges)
    assert "site_similarity_graph" not in await _projections(graph_driver)


@pytest.mark.asyncio
async def test_rerun_replaces_and_cutoff_bounds(graph_driver, domain_database, clean_db, flag_on) -> None:
    await _seed(graph_driver)
    spec = _spec()
    scheduler = GDSScheduler(spec, graph_driver)
    first = await scheduler.execute_job(spec.gdsjobs[0])
    after_first = await _similar(graph_driver)

    second = await scheduler.execute_job(spec.gdsjobs[0])
    assert second["status"] == "success", second
    assert second["relationships_deleted"] == first["relationships_written"]
    assert await _similar(graph_driver) == after_first

    bounded = _spec(cutoff=0.5)
    third = await GDSScheduler(bounded, graph_driver).execute_job(bounded.gdsjobs[0])
    assert third["status"] == "success", third
    assert set(await _similar(graph_driver)) == {("s1", "s2"), ("s2", "s1")}


@pytest.mark.asyncio
async def test_flag_off_writes_nothing(graph_driver, domain_database, clean_db) -> None:
    from engine.config.settings import settings

    assert settings.gds_node_similarity_enabled is False
    await _seed(graph_driver)
    spec = _spec()
    result = await GDSScheduler(spec, graph_driver).execute_job(spec.gdsjobs[0])
    assert result["status"] == "skipped"
    assert await _similar(graph_driver) == {}
