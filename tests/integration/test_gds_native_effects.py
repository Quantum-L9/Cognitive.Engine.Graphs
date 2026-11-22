"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, gds, louvain, cooccurrence, reinforcement, geoproximity, equipmentsync, causal]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 N4 — real graph effects of the declared GDS algorithms.

Every test runs ``GDSScheduler.execute_job`` against a live Neo4j 5 enterprise
with the Graph Data Science plugin (tests/conftest.py ``neo4j_container``)
and reads the graph back before and after. Cases: G01/G02 (dependency barrier
on real writes), G09 louvain, G10 cooccurrence, G11 reinforcement, G12 prune
failure after write, G13 temporal recency after elapsed time, G20
nodeSimilarity pre-swap failure, G21 geoproximity / equipmentsync, G22
causal_chain_scoring / feedback_recalculation dispatch, X06 a second tenant
database left untouched.

Fails under CEG_REQUIRE_INTEGRATION=1 when Neo4j is unavailable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from engine.config.schema import DomainSpec, GDSJobScheduleSpec, GDSJobSpec, GDSNodeSimilaritySpec, GDSProjectionSpec
from engine.config.settings import settings
from engine.gds.node_similarity import staging_edge_for
from engine.gds.scheduler import GDSScheduler

pytestmark = pytest.mark.integration

DB = "orbital-logistics"  # synthetic tenant db: domain_database creates it, clean_db wipes it
OTHER_DB = "orbital-logistics-x06"  # second tenant (X06): created and wiped here

_NODE = {"managedby": "sync", "matchdirection": "d1"}


def _edge(edge_type: str, source: str, target: str) -> dict[str, Any]:
    return {
        "type": edge_type,
        "from": source,
        "to": target,
        "direction": "DIRECTED",
        "category": "transaction",
        "managedby": "sync",
    }


def _spec(
    jobs: list[GDSJobSpec],
    db: str = DB,
    causal: dict[str, Any] | None = None,
    feedbackloop: dict[str, Any] | None = None,
) -> DomainSpec:
    raw: dict[str, Any] = {
        "domain": {"id": db, "name": "GDS native effects", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {**_NODE, "label": "Site", "candidate": True, "properties": [{"name": "sid", "type": "string"}]},
                {**_NODE, "label": "Depot", "properties": [{"name": "sid", "type": "string"}]},
                {**_NODE, "label": "Resource", "properties": [{"name": "rid", "type": "string"}]},
                {**_NODE, "label": "ServiceType", "properties": [{"name": "name", "type": "string"}]},
                {**_NODE, "label": "Outcome", "properties": [{"name": "oid", "type": "string"}]},
                {**_NODE, "label": "Query", "queryentity": True, "properties": [{"name": "qid", "type": "string"}]},
            ],
            "edges": [
                _edge("LINKS", "Site", "Site"),
                _edge("USES", "Site", "Resource"),
                _edge("INTERACTED_WITH", "Site", "Site"),
                _edge("OFFERS", "Site", "ServiceType"),
                _edge("TRIGGERS", "Site", "Site"),
                _edge("CO_USES", "Site", "Site"),
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
        "gdsjobs": [j.model_dump() for j in jobs],
    }
    if causal is not None:
        raw["causal"] = causal
    if feedbackloop is not None:
        raw["feedbackloop"] = feedbackloop
    return DomainSpec(**raw)


def _job(name: str, algorithm: str, edgetypes: list[str], **fields: Any) -> GDSJobSpec:
    return GDSJobSpec(
        name=name,
        algorithm=algorithm,
        schedule=GDSJobScheduleSpec(type="manual"),
        projection=GDSProjectionSpec(nodelabels=fields.pop("nodelabels", ["Site"]), edgetypes=edgetypes),
        **fields,
    )


async def _write(driver: Any, cypher: str, params: dict[str, Any] | None = None, db: str = DB) -> None:
    await driver.execute_write(cypher=cypher, parameters=params or {}, database=db)


async def _rows(driver: Any, cypher: str, params: dict[str, Any] | None = None, db: str = DB) -> list[dict[str, Any]]:
    return await driver.execute_query(cypher, parameters=params or {}, database=db)


async def _relationship_fingerprint(driver: Any, db: str) -> list[tuple[Any, ...]]:
    """Every relationship with its endpoints and properties: a before/after readback."""
    rows = await _rows(
        driver,
        "MATCH (a)-[r]->(b) RETURN coalesce(a.sid, a.rid, a.name) AS a, type(r) AS t, "
        "coalesce(b.sid, b.rid, b.name) AS b, properties(r) AS p",
        db=db,
    )
    return sorted(
        (row["a"], row["t"], row["b"], tuple(sorted((k, str(v)) for k, v in row["p"].items()))) for row in rows
    )


async def _projections(driver: Any, db: str = DB) -> list[str]:
    rows = await _rows(driver, "CALL gds.graph.list() YIELD graphName RETURN graphName", db=db)
    return [row["graphName"] for row in rows]


class _FailingDriver:
    """Delegates to the real driver; raises on the first statement containing ``marker``."""

    def __init__(self, driver: Any, marker: str) -> None:
        self._driver = driver
        self._marker = marker
        self.raised = False

    async def execute_query(self, cypher: str, *args: Any, **kwargs: Any) -> Any:
        if not self.raised and self._marker in cypher:
            self.raised = True
            msg = f"injected failure on statement containing {self._marker!r}"
            raise RuntimeError(msg)
        return await self._driver.execute_query(cypher, *args, **kwargs)


@pytest_asyncio.fixture
async def other_tenant(graph_driver: Any) -> AsyncGenerator[str, None]:
    await graph_driver.execute_query(
        cypher=f"CREATE DATABASE `{OTHER_DB}` IF NOT EXISTS WAIT", parameters={}, database="system"
    )
    await _write(graph_driver, "MATCH (n) DETACH DELETE n", db=OTHER_DB)
    yield OTHER_DB
    await _write(graph_driver, "MATCH (n) DETACH DELETE n", db=OTHER_DB)


# ── G09 louvain ───────────────────────────────────────────────────────────────

TWO_CLIQUES = """
UNWIND range(1, 10) AS i
CREATE (:Site {sid: 's' + i})
WITH count(*) AS _
UNWIND [[1, 5], [6, 10]] AS bounds
UNWIND range(bounds[0], bounds[1]) AS i
UNWIND range(bounds[0], bounds[1]) AS j
WITH i, j WHERE i < j
MATCH (a:Site {sid: 's' + i}), (b:Site {sid: 's' + j})
CREATE (a)-[:LINKS]->(b)
"""


async def _communities(driver: Any, prop: str = "cluster") -> dict[str, Any]:
    rows = await _rows(driver, f"MATCH (s:Site) RETURN s.sid AS sid, s.{prop} AS c")
    return {row["sid"]: row["c"] for row in rows}


def _partition(communities: dict[str, Any]) -> set[frozenset[str]]:
    groups: dict[Any, set[str]] = {}
    for sid, community in communities.items():
        groups.setdefault(community, set()).add(sid)
    return {frozenset(group) for group in groups.values()}


@pytest.mark.asyncio
async def test_g09_louvain_writes_declared_property_cleans_projection_and_repeats(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(graph_driver, TWO_CLIQUES)
    job = _job("site_communities", "louvain", ["LINKS"], writeproperty="cluster")
    spec = _spec([job])
    scheduler = GDSScheduler(spec, graph_driver)

    before = await _communities(graph_driver)
    first = await scheduler.execute_job(job)
    after_first = await _communities(graph_driver)
    second = await scheduler.execute_job(job)
    after_second = await _communities(graph_driver)

    assert set(before.values()) == {None}
    assert first["status"] == "success", first
    assert first["communities"] == 2
    expected = {frozenset(f"s{i}" for i in range(1, 6)), frozenset(f"s{i}" for i in range(6, 11))}
    assert _partition(after_first) == expected
    assert second["status"] == "success", second
    assert _partition(after_second) == expected  # same source graph, same partition
    assert "site_communities_graph" not in await _projections(graph_driver)


@pytest.mark.asyncio
async def test_g09_stability_runs_report_diagnostic_average_not_consensus(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(graph_driver, TWO_CLIQUES)
    job = _job("site_communities", "louvain", ["LINKS"], writeproperty="cluster", stability_runs=3)
    result = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)

    assert result["status"] == "success", result
    assert result["stability_runs"] == 3
    assert result["consensus"] is False
    assert len(_partition(await _communities(graph_driver))) == 2
    assert "site_communities_graph" not in await _projections(graph_driver)


# ── G10 / G12 cooccurrence ───────────────────────────────────────────────────

COOCCURRENCE_SEED = """
UNWIND ['a', 'b', 'c', 'd'] AS sid
CREATE (:Site {sid: sid})
WITH count(*) AS _
UNWIND ['r1', 'r2', 'r3'] AS rid
CREATE (:Resource {rid: rid})
WITH count(*) AS _
UNWIND $uses AS u
MATCH (s:Site {sid: u[0]}), (r:Resource {rid: u[1]})
CREATE (s)-[:USES]->(r)
"""
# a,b share r1,r2,r3 (weight 3); a,c share r1,r2 (2); b,c share r1,r2 (2); d shares only r3 with a,b (1).
USES = [
    ["a", "r1"],
    ["a", "r2"],
    ["a", "r3"],
    ["b", "r1"],
    ["b", "r2"],
    ["b", "r3"],
    ["c", "r1"],
    ["c", "r2"],
    ["d", "r3"],
]


async def _co_edges(driver: Any, db: str = DB) -> dict[frozenset[str], Any]:
    rows = await _rows(
        driver, "MATCH (a)-[r:CO_USES]->(b) RETURN a.sid AS a, b.sid AS b, r.weight AS w, r.owner AS o", db=db
    )
    return {frozenset((row["a"], row["b"])): (row["w"], row["o"]) for row in rows}


@pytest.mark.asyncio
async def test_g10_cooccurrence_exact_weights_threshold_and_foreign_edge_kept(
    graph_driver: Any, domain_database: str, clean_db: None, other_tenant: str
) -> None:
    await _write(graph_driver, COOCCURRENCE_SEED, {"uses": USES})
    await _write(graph_driver, COOCCURRENCE_SEED, {"uses": USES}, db=other_tenant)
    # A relationship of the same type written by someone else between nodes this job never pairs.
    await _write(
        graph_driver,
        "MATCH (c:Site {sid: 'c'}), (d:Site {sid: 'd'}) CREATE (c)-[:CO_USES {weight: 9, owner: 'foreign'}]->(d)",
    )
    other_before = await _relationship_fingerprint(graph_driver, other_tenant)
    job = _job(
        "co_usage", "cooccurrence", ["USES"], nodelabels=["Site", "Resource"], sourceedge="USES", writeedge="CO_USES"
    )

    result = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)

    assert result["status"] == "success", result
    edges = await _co_edges(graph_driver)
    assert edges == {
        frozenset("ab"): (3, None),
        frozenset("ac"): (2, None),
        frozenset("bc"): (2, None),
        frozenset("cd"): (9, "foreign"),  # foreign producer's edge untouched
    }
    assert result["edges_created"] == 3
    assert result["edges_pruned"] == 0  # every degree <= 10: the prune floor keeps all
    assert await _relationship_fingerprint(graph_driver, other_tenant) == other_before  # X06


@pytest.mark.asyncio
async def test_g12_prune_failure_after_write_is_partial_and_edges_stay_for_retry(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(graph_driver, COOCCURRENCE_SEED, {"uses": USES})
    job = _job(
        "co_usage", "cooccurrence", ["USES"], nodelabels=["Site", "Resource"], sourceedge="USES", writeedge="CO_USES"
    )
    failing = _FailingDriver(graph_driver, "DELETE r")
    scheduler = GDSScheduler(_spec([job]), failing)  # type: ignore[arg-type]

    result = await scheduler.execute_job(job)

    assert failing.raised
    assert result["status"] == "partial", result
    assert "injected failure" in result["prune_error"]
    assert set(await _co_edges(graph_driver)) == {frozenset("ab"), frozenset("ac"), frozenset("bc")}
    assert scheduler.job_history[-1]["status"] == "partial"
    retry = await scheduler.execute_job(job)  # the injected failure fires once
    assert retry["status"] == "success", retry


# ── G11 reinforcement ────────────────────────────────────────────────────────

REINFORCEMENT_SEED = """
UNWIND ['a', 'b', 'c'] AS sid
CREATE (:Site {sid: sid})
WITH count(*) AS _
UNWIND $events AS e
MATCH (s:Site {sid: e[0]}), (t:Site {sid: e[1]})
CREATE (s)-[:INTERACTED_WITH {result: e[2], at: datetime() - duration({days: e[3]})}]->(t)
"""
EVENTS = [
    ["a", "b", "won", 1],
    ["a", "b", "won", 10],
    ["a", "b", "lost", 5],
    ["a", "c", "lost", 2],
    ["a", "c", "lost", 3],
    ["b", "c", "pending", 1],
]


@pytest.mark.asyncio
async def test_g11_reinforcement_signed_edge_families_and_counts(
    graph_driver: Any, domain_database: str, clean_db: None, other_tenant: str
) -> None:
    await _write(graph_driver, REINFORCEMENT_SEED, {"events": EVENTS})
    await _write(graph_driver, REINFORCEMENT_SEED, {"events": EVENTS}, db=other_tenant)
    other_before = await _relationship_fingerprint(graph_driver, other_tenant)
    job = _job(
        "outcomes",
        "reinforcement",
        ["INTERACTED_WITH"],
        sourceedge="INTERACTED_WITH",
        writeedge="REINFORCED_BY",
        writeproperties=[
            {"negative_writeedge": "DISCOURAGED_BY"},
            {"outcome_property": "result"},
            {"success_value": "won"},
            {"failure_value": "lost"},
            {"timestamp_property": "at"},
        ],
    )

    result = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)

    assert result["status"] == "success", result
    positive = await _rows(
        graph_driver,
        "MATCH (a)-[r:REINFORCED_BY]->(b) RETURN a.sid AS a, b.sid AS b, r.count AS n, r.success_rate AS rate, "
        "r.recency_score AS score, r.last_event_date IS NOT NULL AS dated",
    )
    negative = await _rows(
        graph_driver,
        "MATCH (a)-[r:DISCOURAGED_BY]->(b) RETURN a.sid AS a, b.sid AS b, r.count AS n, r.recency_score AS score",
    )
    assert [(r["a"], r["b"], r["n"], r["dated"]) for r in positive] == [("a", "b", 2, True)]
    assert positive[0]["rate"] == pytest.approx(2 / 3)
    assert 0.99 < positive[0]["score"] <= 1.0  # newest win 1 day old, half-life 180
    assert sorted((r["a"], r["b"], r["n"]) for r in negative) == [("a", "b", 1), ("a", "c", 2)]
    assert all(r["score"] > 0.9 for r in negative)  # negative signals are kept, not pruned
    assert result == {**result, "accepted_edges": 1, "rejected_edges": 2}
    assert await _relationship_fingerprint(graph_driver, other_tenant) == other_before  # X06


# ── G13 temporal recency after elapsed event age ─────────────────────────────


@pytest.mark.asyncio
async def test_g13_temporal_recency_declines_then_prunes_as_events_age(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(
        graph_driver,
        "CREATE (a:Site {sid: 'a'}), (b:Site {sid: 'b'}) "
        "CREATE (a)-[:INTERACTED_WITH {at: datetime() - duration({days: 30})}]->(b) "
        "CREATE (a)-[:INTERACTED_WITH {at: datetime() - duration({days: 5})}]->(b)",
    )
    job = _job(
        "recency",
        "temporalrecency",
        ["INTERACTED_WITH"],
        sourceedge="INTERACTED_WITH",
        writeedge="RECENT",
        writeproperties=[{"timestamp_property": "at"}, {"half_life_days": 30}, {"max_age_days": 120}],
    )
    scheduler = GDSScheduler(_spec([job]), graph_driver)

    async def _overlay() -> list[dict[str, Any]]:
        return await _rows(
            graph_driver,
            "MATCH (:Site)-[r:RECENT]->(:Site) RETURN r.recency_score AS score, "
            "duration.inDays(r.last_event_date, datetime()).days AS age",
        )

    assert (await scheduler.execute_job(job))["status"] == "success"
    fresh = await _overlay()
    assert len(fresh) == 1
    assert fresh[0]["age"] == 5  # newest interaction wins
    # Elapsed time: every interaction is now 60 days older.
    await _write(graph_driver, "MATCH ()-[t:INTERACTED_WITH]->() SET t.at = t.at - duration({days: 60})")
    assert (await scheduler.execute_job(job))["status"] == "success"
    aged = await _overlay()
    assert aged[0]["age"] == 65
    assert aged[0]["score"] < fresh[0]["score"]
    # 90 more days elapse: source events and the overlay's copy of them age alike.
    await _write(graph_driver, "MATCH ()-[t:INTERACTED_WITH]->() SET t.at = t.at - duration({days: 90})")
    await _write(
        graph_driver, "MATCH ()-[r:RECENT]->() SET r.last_event_date = r.last_event_date - duration({days: 90})"
    )
    result = await scheduler.execute_job(job)
    assert result["status"] == "success"
    assert result["edges_pruned"] == 1
    assert await _overlay() == []


# ── G21 geoproximity / equipmentsync ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_g21_geoproximity_declared_coordinates_only_one_edge_per_pair(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(
        graph_driver,
        "CREATE (:Site {sid: 'nyc', geo: 'g1', la: 40.7128, lo: -74.0060}), "
        "(:Site {sid: 'nwk', geo: 'g2', la: 40.7357, lo: -74.1724}), "
        "(:Site {sid: 'phl', geo: 'g3', la: 39.9526, lo: -75.1652}), "
        "(:Site {sid: 'lax', geo: 'g4', la: 34.0522, lo: -118.2437}), "
        "(:Site {sid: 'nolat', geo: 'g5', lo: -74.0}), "
        "(:Site {sid: 'nolon', geo: 'g6', la: 40.7}), "
        "(:Site {sid: 'noid', la: 40.71, lo: -74.01}), "
        # Undeclared vocabulary: must never be read.
        "(:Site {sid: 'other', geo: 'g7', lat: 40.7128, lon: -74.0060})",
    )
    job = _job(
        "near",
        "geoproximity",
        ["LINKS"],
        writeedge="NEAR",
        writeproperties=[{"lat_property": "la"}, {"lon_property": "lo"}, {"id_property": "geo"}, {"max_km": 200}],
    )

    result = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)

    assert result["status"] == "success", result
    rows = await _rows(graph_driver, "MATCH (a)-[r:NEAR]->(b) RETURN a.sid AS a, b.sid AS b, r.distance_km AS km")
    pairs = {frozenset((r["a"], r["b"])): r["km"] for r in rows}
    assert set(pairs) == {frozenset(("nyc", "nwk")), frozenset(("nyc", "phl")), frozenset(("nwk", "phl"))}
    assert len(rows) == 3  # one directed edge per pair, no reciprocal duplicate
    assert pairs[frozenset(("nyc", "phl"))] == pytest.approx(129.6, abs=1.0)


@pytest.mark.asyncio
async def test_g21_equipmentsync_declared_boolean_mapping_only(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(
        graph_driver,
        "CREATE (:ServiceType {name: 'cold chain'}), (:ServiceType {name: 'crane'}), (:ServiceType {name: 'dock'}) "
        "CREATE (:Site {sid: 'true_bool', has_cold: true, has_crane: true}), "
        "(:Site {sid: 'false_bool', has_cold: false}), "
        "(:Site {sid: 'string_true', has_cold: 'true'}), "
        "(:Site {sid: 'int_one', has_cold: 1}), "
        "(:Site {sid: 'missing'}), "
        "(:Site {sid: 'undeclared', has_dock: true})",
    )
    job = _job(
        "services",
        "equipmentsync",
        ["OFFERS"],
        writeedge="OFFERS",
        writeto="ServiceType",
        writeproperties=[{"property_mappings": {"has_cold": "cold chain", "has_crane": "crane"}}],
    )

    result = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)

    assert result["status"] == "success", result
    rows = await _rows(graph_driver, "MATCH (s:Site)-[:OFFERS]->(t:ServiceType) RETURN s.sid AS s, t.name AS t")
    assert sorted((r["s"], r["t"]) for r in rows) == [("true_bool", "cold chain"), ("true_bool", "crane")]
    again = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)
    assert again["status"] == "success"
    assert len(await _rows(graph_driver, "MATCH (:Site)-[r:OFFERS]->() RETURN r")) == 2  # MERGE: idempotent


# ── G22 causal_chain_scoring / feedback_recalculation ────────────────────────


@pytest.mark.asyncio
async def test_g22_causal_chain_scoring_follows_declared_causal_edges_only(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    await _write(
        graph_driver,
        "CREATE (a:Site {sid: 'a'}), (b:Site {sid: 'b'}), (c:Site {sid: 'c'}), (x:Site {sid: 'x'}) "
        "CREATE (a)-[:TRIGGERS {confidence: 0.8}]->(b)-[:TRIGGERS {confidence: 0.5}]->(c) "
        "CREATE (x)-[:LINKS]->(a)",  # not a causal edge: must not make x a cause
    )
    causal = {
        "enabled": True,
        "causal_edges": [{"edge_type": "TRIGGERS", "source_label": "Site", "target_label": "Site"}],
    }
    job = _job("influence", "causal_chain_scoring", ["TRIGGERS"], writeproperty="influence")

    result = await GDSScheduler(_spec([job], causal=causal), graph_driver).execute_job(job)

    assert result["status"] == "success", result
    assert result["nodes_scored"] == 4
    rows = await _rows(graph_driver, "MATCH (s:Site) RETURN s.sid AS sid, s.influence AS score")
    scores = {r["sid"]: r["score"] for r in rows}
    # a: 2 downstream effects, depth 2, avg confidence (0.8 + 0.4) / 2 = 0.6
    assert scores["a"] == pytest.approx(2 * 0.4 + 2 * 0.3 + 0.6 * 0.3)
    assert scores["b"] == pytest.approx(1 * 0.4 + 1 * 0.3 + 0.5 * 0.3)
    assert scores["c"] == 0.0
    assert scores["x"] == 0.0


@pytest.mark.asyncio
async def test_g22_causal_chain_scoring_without_declared_causal_edges(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    """No declared causal edge type: arbitrary relationships must not be read as causation."""
    await _write(graph_driver, "CREATE (x:Site {sid: 'x'})-[:LINKS]->(:Site {sid: 'a'})")
    job = _job("influence", "causal_chain_scoring", ["LINKS"], writeproperty="influence")

    result = await GDSScheduler(_spec([job]), graph_driver).execute_job(job)

    rows = await _rows(graph_driver, "MATCH (s:Site {sid: 'x'}) RETURN s.influence AS score")
    assert result["status"] == "failed", result
    assert "causal" in result["error"]
    assert rows[0]["score"] is None  # nothing written


@pytest.mark.asyncio
async def test_g22_feedback_recalculation_dispatch_is_visible_and_flag_off_applies_nothing(
    graph_driver: Any, domain_database: str, clean_db: None
) -> None:
    """Dispatch only: the weight writer and its flags belong to the feedback lane."""
    assert settings.learned_weight_auto_apply_enabled is False
    before = await _rows(graph_driver, "MATCH (w:DimensionWeight) RETURN count(w) AS n")
    job = _job("weights", "feedback_recalculation", ["LINKS"])
    feedbackloop = {"enabled": True, "outcome_node_label": "Outcome"}
    scheduler = GDSScheduler(_spec([job], feedbackloop=feedbackloop), graph_driver)

    result = await scheduler.execute_job(job)

    after = await _rows(graph_driver, "MATCH (w:DimensionWeight) RETURN count(w) AS n")
    assert result["status"] == "success", result
    assert result["algorithm"] == "feedback_recalculation"
    assert result["weights_updated"] == 0  # auto-apply off: computed, not applied
    assert after == before
    assert scheduler.job_history[-1]["job"] == "weights"


# ── G20 nodeSimilarity pre-swap failure ──────────────────────────────────────

SIMILARITY_SEED = """
UNWIND $uses AS u
MERGE (s:Site {sid: u[0]})
MERGE (r:Resource {rid: u[1]})
MERGE (s)-[:USES]->(r)
"""


async def _similar(driver: Any, db: str = DB) -> dict[tuple[str, str], Any]:
    rows = await _rows(driver, "MATCH (a:Site)-[r:SIMILAR]->(b:Site) RETURN a.sid AS a, b.sid AS b, r.sim AS s", db=db)
    return {(r["a"], r["b"]): r["s"] for r in rows}


@pytest.mark.asyncio
async def test_g20_node_similarity_keeps_last_good_output_on_pre_swap_failure(
    graph_driver: Any, domain_database: str, clean_db: None, other_tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gds_node_similarity_enabled", True)
    await _write(graph_driver, SIMILARITY_SEED, {"uses": USES})
    await _write(graph_driver, SIMILARITY_SEED, {"uses": USES}, db=other_tenant)
    await _write(graph_driver, "MATCH (c:Site {sid: 'c'}), (d:Site {sid: 'd'}) CREATE (c)-[:SIMILAR {sim: 0.99}]->(d)")
    other_before = await _relationship_fingerprint(graph_driver, other_tenant)
    job = _job(
        "site_similarity",
        "nodeSimilarity",
        ["USES"],
        nodelabels=["Site", "Resource"],
        writeedge="SIMILAR",
        writeproperty="sim",
        nodesimilarity=GDSNodeSimilaritySpec(topk=2),
    )
    spec = _spec([job])

    first = await GDSScheduler(spec, graph_driver).execute_job(job)
    good = await _similar(graph_driver)
    assert first["status"] == "success", first
    assert first["relationships_written"] == len(good) - 1  # + the foreign edge
    owned = {pair for pair in good if pair != ("c", "d")}
    assert all(sum(1 for (a, _b) in owned if a == sid) <= 2 for sid in "abcd")  # topK bound

    # New source data, then a failure after the staging write but before the swap.
    await _write(graph_driver, "MATCH (d:Site {sid: 'd'}), (r:Resource {rid: 'r1'}) MERGE (d)-[:USES]->(r)")
    failing = _FailingDriver(graph_driver, "DELETE old")
    failed = await GDSScheduler(spec, failing).execute_job(job)  # type: ignore[arg-type]

    assert failing.raised, "swap statement marker not found; the injection did not fire"
    assert failed["status"] == "failed", failed
    assert await _similar(graph_driver) == good  # last good output survives
    assert "site_similarity_graph" not in await _projections(graph_driver)
    stage = staging_edge_for("site_similarity")
    staged = await _rows(graph_driver, f"MATCH ()-[s:{stage}]->() RETURN count(s) AS n")
    assert staged[0]["n"] > 0  # the abandoned staging edges are what the next run clears

    retry = await GDSScheduler(spec, graph_driver).execute_job(job)
    assert retry["status"] == "success", retry
    assert (await _rows(graph_driver, f"MATCH ()-[s:{stage}]->() RETURN count(s) AS n"))[0]["n"] == 0
    assert (await _similar(graph_driver))[("c", "d")] == pytest.approx(0.99)  # foreign edge kept
    assert await _relationship_fingerprint(graph_driver, other_tenant) == other_before  # X06


# ── G01 / G02 barrier on real graph writes ───────────────────────────────────


@pytest.mark.asyncio
async def test_g01_g02_dependent_reads_producer_output_and_is_blocked_after_failure(
    graph_driver: Any, domain_database: str, clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dependent's graph effect starts after its producer's write; a failed producer blocks it."""
    monkeypatch.setattr(settings, "gds_dependency_barrier_enabled", True)
    await _write(graph_driver, COOCCURRENCE_SEED, {"uses": USES})
    producer = _job(
        "co_usage", "cooccurrence", ["USES"], nodelabels=["Site", "Resource"], sourceedge="USES", writeedge="CO_USES"
    )
    dependent = _job("co_communities", "louvain", ["CO_USES"], writeproperty="cluster", depends_on=["cooccurrence"])
    spec = _spec([producer, dependent])
    scheduler = GDSScheduler(spec, graph_driver)

    blocked = await scheduler.execute_job(dependent)
    assert blocked["status"] == "blocked"
    assert set((await _communities(graph_driver)).values()) == {None}  # no write before the producer

    assert (await scheduler.execute_job(producer))["status"] == "success"
    ran = await scheduler.execute_job(dependent)
    assert ran["status"] == "success", ran
    communities = await _communities(graph_driver)
    assert communities["a"] == communities["b"] == communities["c"]  # over the CO_USES the producer wrote

    failing = _FailingDriver(graph_driver, "MERGE (a)-[r:CO_USES")
    failing_scheduler = GDSScheduler(spec, failing)  # type: ignore[arg-type]
    assert (await failing_scheduler.execute_job(producer))["status"] == "failed"
    await _write(graph_driver, "MATCH (s:Site) REMOVE s.cluster")
    after_failure = await failing_scheduler.execute_job(dependent)
    assert after_failure["status"] == "blocked"
    assert after_failure["unsatisfied"] == ["co_usage: last attempt failed"]
    assert set((await _communities(graph_driver)).values()) == {None}


TWO_HUBS = """
UNWIND ['H1', 'H2'] AS h
CREATE (hub:Site {sid: h})
WITH hub
UNWIND range(1, 12) AS i
CREATE (p:Site {sid: hub.sid + '_p' + i}), (r1:Resource), (r2:Resource)
CREATE (hub)-[:USES]->(r1), (hub)-[:USES]->(r2), (p)-[:USES]->(r1), (p)-[:USES]->(r2)
"""


@pytest.mark.asyncio
async def test_g10_x06_cooccurrence_prune_spares_foreign_edges_and_stays_in_declared_label(
    graph_driver: Any, domain_database: str, clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two degree-12 hubs joined by another producer's weak CO_USES edge, plus Depot nodes sharing resources."""
    monkeypatch.setattr(settings, "gds_cooccurrence_owned_edges_enabled", True)
    await _write(graph_driver, TWO_HUBS)
    await _write(
        graph_driver,
        "MATCH (a:Site {sid: 'H1'}), (b:Site {sid: 'H2'}) CREATE (a)-[:CO_USES {weight: 1, owner: 'foreign'}]->(b)",
    )
    await _write(
        graph_driver,
        "CREATE (x:Depot {sid: 'dx'}), (y:Depot {sid: 'dy'}), (r1:Resource), (r2:Resource) "
        "CREATE (x)-[:USES]->(r1), (x)-[:USES]->(r2), (y)-[:USES]->(r1), (y)-[:USES]->(r2)",
    )
    job = _job(
        "co_usage", "cooccurrence", ["USES"], nodelabels=["Site", "Resource"], sourceedge="USES", writeedge="CO_USES"
    )
    scheduler = GDSScheduler(_spec([job]), graph_driver)

    first = await scheduler.execute_job(job)
    second = await scheduler.execute_job(job)

    assert first["status"] == "success", first
    foreign = await _rows(graph_driver, "MATCH (:Site)-[r:CO_USES {owner: 'foreign'}]->(:Site) RETURN r.weight AS w")
    assert foreign == [{"w": 1}]  # another producer's edge is neither pruned nor re-weighted
    depot = await _rows(graph_driver, "MATCH (:Depot)-[r:CO_USES]->(:Depot) RETURN count(r) AS n")
    assert depot == [{"n": 0}]  # the job's declared label is Site
    owned = await _rows(
        graph_driver, "MATCH (:Site)-[r:CO_USES]->(:Site) WHERE r.gds_job = 'co_usage' RETURN count(r) AS n"
    )
    assert owned == [{"n": 24}]  # 12 per hub; every hub keeps floor(12^0.86) >= 8, leaves keep theirs
    assert first["edges_pruned"] == 0
    assert second["status"] == "success", second
    assert second["edges_created"] == 24  # MERGE on the owner marker: no duplicates on rerun
