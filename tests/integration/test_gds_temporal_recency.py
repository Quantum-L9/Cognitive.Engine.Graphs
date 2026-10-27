"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, gds, temporal-recency]
owner: engine-team
status: active
--- /L9_META ---

Integration test — GDS temporal-recency overlay (RU-097 / CEG-COR-037).

Seeds interaction edges at known ages and runs the ``temporalrecency`` job
against a live Neo4j (testcontainers, as test_causal_temporal_decay.py):

  * a pair with interactions 5 and 100 days old gets one overlay edge whose
    ``last_event_date`` is the 5-day interaction (the newest), not whichever
    row Neo4j happened to SET last;
  * an overlay edge whose newest interaction is 300 days old (older than the
    default ``max_age_days`` of 207) is deleted on the next run, even though
    the create step no longer touches it;
  * a second run is stable.

Requires a live Neo4j; skips automatically when testcontainers / NEO4J_TEST_URI
is unavailable (see tests/conftest.py::neo4j_container).
"""

from __future__ import annotations

import pytest

from engine.config.schema import DomainSpec, GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.gds.scheduler import GDSScheduler

DB = "plasticos"  # created by the plasticos_database fixture; wiped by clean_db

SEED_CYPHER = """
MERGE (a:Site {site_id: 1})
MERGE (b:Site {site_id: 2})
MERGE (c:Site {site_id: 3})
CREATE (a)-[:INTERACTED_WITH {last_seen_at: datetime() - duration({days: 5})}]->(b)
CREATE (a)-[:INTERACTED_WITH {last_seen_at: datetime() - duration({days: 100})}]->(b)
CREATE (a)-[:INTERACTED_WITH {last_seen_at: datetime() - duration({days: 300})}]->(c)
// An overlay edge left by an earlier run, frozen at a score above prune_below.
CREATE (a)-[:RECENTLY_INTERACTED_WITH {
    last_event_date: datetime() - duration({days: 300}),
    recency_score: 0.5
}]->(c)
"""


def _spec() -> DomainSpec:
    job = GDSJobSpec(
        name="recency_overlay",
        algorithm="temporalrecency",
        schedule=GDSJobScheduleSpec(type="manual"),
        projection=GDSProjectionSpec(nodelabels=["Site"], edgetypes=["INTERACTED_WITH"]),
        sourceedge="INTERACTED_WITH",
        writeedge="RECENTLY_INTERACTED_WITH",
        writeproperties=[{"timestamp_property": "last_seen_at"}],
    )
    raw = {
        "domain": {"id": DB, "name": "Recency", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {
                    "label": "Site",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "site_id", "type": "int", "required": True}],
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
                    "type": "INTERACTED_WITH",
                    "from": "Site",
                    "to": "Site",
                    "direction": "DIRECTED",
                    "category": "transaction",
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


async def _overlay(graph_driver) -> dict[int, dict]:
    rows = await graph_driver.execute_query(
        """
        MATCH (:Site {site_id: 1})-[r:RECENTLY_INTERACTED_WITH]->(d:Site)
        OPTIONAL MATCH (:Site {site_id: 1})-[t:INTERACTED_WITH]->(d)
        WITH d, r, max(t.last_seen_at) AS newest
        RETURN d.site_id AS dst, r.last_event_date = newest AS is_newest,
               duration.inDays(r.last_event_date, datetime()).days AS age_days,
               r.recency_score AS score
        """,
        parameters={},
        database=DB,
    )
    return {row["dst"]: row for row in rows}


@pytest.mark.asyncio
async def test_overlay_keeps_newest_interaction_and_prunes_aged_edges(
    graph_driver, plasticos_database, clean_db
) -> None:
    from engine.config.settings import settings

    assert settings.gds_temporal_recency_decay_existing is True
    await graph_driver.execute_write(cypher=SEED_CYPHER, parameters={}, database=DB)
    spec = _spec()
    scheduler = GDSScheduler(spec, graph_driver)

    first = await scheduler.execute_job(spec.gdsjobs[0])
    assert first["status"] == "success"
    overlay = await _overlay(graph_driver)

    assert set(overlay) == {2}, "the 300-day-old overlay edge must be pruned"
    assert overlay[2]["is_newest"] is True
    assert overlay[2]["age_days"] == 5
    assert first["edges_created"] == 1

    second = await scheduler.execute_job(spec.gdsjobs[0])
    assert second["status"] == "success"
    assert await _overlay(graph_driver) == overlay
