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

import math

import pytest

from engine.config.schema import DomainSpec, GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.gds.scheduler import GDSScheduler
from tests.unit.test_gds_scheduler import _minimal_spec

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
    # The scheduler unit tests' Site/Query ontology, bound to the integration database.
    return _minimal_spec([job], domain_id=DB)


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


SHARED_WRITEEDGE_SEED = """
MERGE (a:Site {site_id: 1})
MERGE (b:Site {site_id: 2})
MERGE (q1:Query {query_id: 1})
MERGE (q2:Query {query_id: 2})
CREATE (a)-[:INTERACTED_WITH {last_seen_at: datetime() - duration({days: 60})}]->(b)
CREATE (q1)-[:INTERACTED_WITH {last_seen_at: datetime() - duration({days: 5})}]->(q2)
"""


def _shared_writeedge_spec() -> DomainSpec:
    """Two temporal-recency jobs, two node labels, one ``writeedge`` (the schema allows it)."""

    def job(name: str, label: str, settings: list[dict]) -> GDSJobSpec:
        return GDSJobSpec(
            name=name,
            algorithm="temporalrecency",
            schedule=GDSJobScheduleSpec(type="manual"),
            projection=GDSProjectionSpec(nodelabels=[label], edgetypes=["INTERACTED_WITH"]),
            sourceedge="INTERACTED_WITH",
            writeedge="RECENTLY_INTERACTED_WITH",
            writeproperties=[{"timestamp_property": "last_seen_at"}, *settings],
        )

    return _minimal_spec(
        [
            job("site_recency", "Site", []),  # defaults: half_life 90, max_age 207
            job("query_recency", "Query", [{"half_life_days": 10.0}, {"max_age_days": 30.0}]),
        ],
        domain_id=DB,
    )


@pytest.mark.asyncio
async def test_jobs_sharing_a_writeedge_only_maintain_their_own_label(
    graph_driver, plasticos_database, clean_db
) -> None:
    """A job's refresh and prune touch only overlay edges between its own node label."""
    await graph_driver.execute_write(cypher=SHARED_WRITEEDGE_SEED, parameters={}, database=DB)
    spec = _shared_writeedge_spec()
    scheduler = GDSScheduler(spec, graph_driver)

    site_job, query_job = spec.gdsjobs
    assert (await scheduler.execute_job(site_job))["status"] == "success"
    query_run = await scheduler.execute_job(query_job)
    assert query_run["status"] == "success"
    assert query_run["edges_pruned"] == 0

    rows = await graph_driver.execute_query(
        """
        MATCH (s)-[r:RECENTLY_INTERACTED_WITH]->()
        RETURN labels(s)[0] AS label, r.recency_score AS score
        """,
        parameters={},
        database=DB,
    )
    scores = {row["label"]: row["score"] for row in rows}
    # The 60-day-old Site edge is inside the Site job's max_age_days (207) and keeps
    # the Site job's half-life (90), not the Query job's 10-day half-life / 30-day age.
    assert set(scores) == {"Site", "Query"}
    assert scores["Site"] == pytest.approx(math.exp(-60 / 90))
    assert scores["Query"] == pytest.approx(math.exp(-5 / 10))
