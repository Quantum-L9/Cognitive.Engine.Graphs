"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, resolution, entity-resolver, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

Integration tests -- entity resolution on the domain's declared identity
(CEG-NATIVE-CLOSURE-001, slices N1/N2, findings F01/F02/F03, cases R01-R14, X05).

Runs on the synthetic orbital-logistics pack (ADR-112 Stage 4): OrbitalDepot is
keyed by its declared ``depot_id`` (sync ``idproperty``) and carries no
``entity_id``; the pack declares ``feedbackloop.outcome_node_label:
DockingResult``. Every write is read back from a real Neo4j.

Requires a live Neo4j (tests/conftest.py::neo4j_container); with
CEG_REQUIRE_INTEGRATION=1 an unavailable Neo4j fails instead of skipping.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.config.schema import DomainSpec, SemanticRegistrySpec
from engine.resolution.resolver import EntityResolver
from engine.resolution.similarity import SimilarityScorer

pytestmark = pytest.mark.integration

DB = "neo4j"  # wiped by clean_db
ORBITAL = "orbital-logistics"
LABEL = "OrbitalDepot"


@pytest.fixture
def orbital_spec(domain_loader) -> DomainSpec:
    return domain_loader.load_domain(ORBITAL)


def _registry(
    *,
    threshold: float = 0.85,
    property_weight: float = 0.5,
    structural_weight: float = 0.3,
    behavioral_weight: float = 0.2,
    comparison_properties: list[str] | None = None,
    max_candidates: int = 20,
) -> SemanticRegistrySpec:
    return SemanticRegistrySpec(
        enabled=True,
        entity_labels=[LABEL],
        similarity_threshold=threshold,
        property_weight=property_weight,
        structural_weight=structural_weight,
        behavioral_weight=behavioral_weight,
        comparison_properties=comparison_properties if comparison_properties is not None else ["docking_ports"],
        max_candidates=max_candidates,
    )


async def _write(graph_driver: Any, cypher: str, params: dict[str, Any] | None = None, db: str = DB) -> None:
    await graph_driver.execute_write(cypher=cypher, parameters=params or {}, database=db)


async def _read(graph_driver: Any, cypher: str, params: dict[str, Any] | None = None, db: str = DB) -> list[Any]:
    return await graph_driver.execute_query(cypher=cypher, parameters=params or {}, database=db)


async def _links(graph_driver: Any, db: str = DB) -> list[tuple[str, str]]:
    rows = await _read(
        graph_driver,
        "MATCH (a:OrbitalDepot)-[:RESOLVED_FROM]->(b:OrbitalDepot) RETURN a.depot_id AS src, b.depot_id AS dst",
        db=db,
    )
    return sorted((r["src"], r["dst"]) for r in rows)


async def _snapshot(graph_driver: Any, db: str = DB) -> dict[str, Any]:
    """Every node's labels/properties and every non-RESOLVED_FROM relationship, sorted."""
    nodes = await _read(graph_driver, "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props", db=db)
    rels = await _read(
        graph_driver,
        """
        MATCH (a)-[r]->(b) WHERE type(r) <> 'RESOLVED_FROM'
        RETURN properties(a) AS a, type(r) AS t, properties(r) AS p, properties(b) AS b
        """,
        db=db,
    )
    return {
        "nodes": sorted(repr((sorted(r["labels"]), sorted(dict(r["props"]).items()))) for r in nodes),
        "rels": sorted(
            repr((sorted(dict(r["a"]).items()), r["t"], sorted(dict(r["p"]).items()), sorted(dict(r["b"]).items())))
            for r in rels
        ),
    }


# Three depots keyed by depot_id only (no entity_id): d_hub has the highest
# non-resolution degree, all share docking_ports and two Thing neighbours.
DUPLICATES_SEED = """
CREATE (h:OrbitalDepot {depot_id: 'd_hub', docking_ports: 6, reliability: 0.9})
CREATE (a:OrbitalDepot {depot_id: 'd_a', docking_ports: 6, reliability: 0.9})
CREATE (b:OrbitalDepot {depot_id: 'd_b', docking_ports: 6, reliability: 0.9})
CREATE (n1:Thing {thing_id: 1}), (n2:Thing {thing_id: 2}), (n3:Thing {thing_id: 3})
CREATE (h)-[:LINKS]->(n1), (h)-[:LINKS]->(n2), (h)-[:LINKS]->(n3)
CREATE (a)-[:LINKS]->(n1), (a)-[:LINKS]->(n2), (a)-[:LINKS]->(n3)
CREATE (b)-[:LINKS]->(n1), (b)-[:LINKS]->(n2), (b)-[:LINKS]->(n3)
"""


# ── N1: similarity on the declared identity ──────────────────────────────────


@pytest.mark.asyncio
async def test_r01_property_candidates_use_the_declared_id(graph_driver, clean_db, orbital_spec) -> None:
    """R01: nodes keyed only by depot_id are found; no phantom/null id is emitted."""
    await _write(
        graph_driver,
        """
        CREATE (:OrbitalDepot {depot_id: 'd1', docking_ports: 6})
        CREATE (:OrbitalDepot {depot_id: 'd2', docking_ports: 6})
        CREATE (:OrbitalDepot {docking_ports: 6})
        CREATE (:OrbitalDepot {depot_id: 'd3', docking_ports: 2})
        """,
    )
    scorer = SimilarityScorer(_registry(), graph_driver, DB, domain_spec=orbital_spec)
    ids = await scorer._find_property_candidates("d1", LABEL, 20)
    assert ids == ["d2"]


@pytest.mark.asyncio
async def test_r02_structural_similarity_uses_the_declared_id(graph_driver, clean_db, orbital_spec) -> None:
    """R02: exact shared-neighbour ratio on depot_id-keyed nodes (2 shared / 4 union)."""
    await _write(
        graph_driver,
        """
        CREATE (a:OrbitalDepot {depot_id: 'd1'}), (b:OrbitalDepot {depot_id: 'd2'})
        CREATE (n1:Thing {thing_id: 1}), (n2:Thing {thing_id: 2}), (n3:Thing {thing_id: 3}), (n4:Thing {thing_id: 4})
        CREATE (a)-[:LINKS]->(n1), (a)-[:LINKS]->(n2), (a)-[:LINKS]->(n3)
        CREATE (b)-[:LINKS]->(n1), (b)-[:LINKS]->(n2), (b)-[:LINKS]->(n4)
        """,
    )
    scorer = SimilarityScorer(_registry(), graph_driver, DB, domain_spec=orbital_spec)
    assert await scorer._structural_similarity("d1", "d2", LABEL) == pytest.approx(2 / 4)


@pytest.mark.asyncio
async def test_r03_nonmatching_properties_with_shared_neighbour(graph_driver, clean_db, orbital_spec) -> None:
    """R03: property term 0 (values differ), structural term 1/1; score = 0.3 * 1.0 exactly."""
    await _write(
        graph_driver,
        """
        CREATE (a:OrbitalDepot {depot_id: 'd1', docking_ports: 6}), (b:OrbitalDepot {depot_id: 'd2', docking_ports: 2})
        CREATE (n:Thing {thing_id: 1}), (a)-[:LINKS]->(n), (b)-[:LINKS]->(n)
        """,
    )
    scorer = SimilarityScorer(_registry(), graph_driver, DB, domain_spec=orbital_spec)
    assert await scorer._property_similarity("d1", "d2", LABEL) == 0.0
    assert await scorer.compute_similarity("d1", "d2", LABEL) == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_r04_null_or_absent_identifier_writes_nothing(graph_driver, clean_db, orbital_spec) -> None:
    """R04: an id-less twin never becomes a candidate or a link endpoint."""
    await _write(
        graph_driver,
        """
        CREATE (:OrbitalDepot {depot_id: 'd1', docking_ports: 6})
        CREATE (:OrbitalDepot {docking_ports: 6})
        CREATE (:OrbitalDepot {depot_id: null, docking_ports: 6})
        """,
    )
    resolver = EntityResolver(_registry(threshold=0.4), graph_driver, DB, domain_spec=orbital_spec)
    single = await resolver.resolve_entity("d1", LABEL)
    absent = await resolver.resolve_entity("d_missing", LABEL)
    batch = await resolver.resolve_batch(LABEL)
    rows = await _read(graph_driver, "MATCH ()-[r:RESOLVED_FROM]->() RETURN count(r) AS n")
    assert single["merged_count"] == 0
    assert single["merged_entity_ids"] == []
    assert absent["merged_count"] == 0
    assert batch["total_entities"] == 1
    assert batch["total_merged"] == 0
    assert rows[0]["n"] == 0


@pytest.mark.asyncio
async def test_r07_same_id_on_another_label_does_not_contaminate(graph_driver, clean_db, orbital_spec) -> None:
    """R07: a LaunchWindow sharing an id string contributes no property, neighbour or outcome."""
    await _write(
        graph_driver,
        """
        CREATE (a:OrbitalDepot {depot_id: 'd1', docking_ports: 6}), (b:OrbitalDepot {depot_id: 'd2', docking_ports: 6})
        CREATE (x:LaunchWindow {depot_id: 'd1', window_id: 'd1', docking_ports: 6})
        CREATE (n1:Thing {thing_id: 1}), (n2:Thing {thing_id: 2})
        CREATE (a)-[:LINKS]->(n1), (b)-[:LINKS]->(n1), (x)-[:LINKS]->(n2)
        CREATE (o:DockingResult {match_id: 'm1'}), (a)-[:RESULTED_IN]->(o), (b)-[:RESULTED_IN]->(o)
        CREATE (x)-[:RESULTED_IN]->(:DockingResult {match_id: 'm9'})
        """,
    )
    scorer = SimilarityScorer(_registry(), graph_driver, DB, domain_spec=orbital_spec)
    assert await scorer._structural_similarity("d1", "d2", LABEL) == pytest.approx(1.0)
    assert await scorer._behavioral_similarity("d1", "d2", LABEL) == pytest.approx(1.0)
    assert await scorer.compute_similarity("d1", "d2", LABEL) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_r09_behavioral_reads_the_declared_outcome_label(graph_driver, clean_db, orbital_spec) -> None:
    """R09: DockingResult outcomes give 1 shared / 3 union; TransactionOutcome nodes are not read."""
    await _write(
        graph_driver,
        """
        CREATE (a:OrbitalDepot {depot_id: 'd1'}), (b:OrbitalDepot {depot_id: 'd2'})
        CREATE (a)-[:RESULTED_IN]->(:DockingResult {match_id: 'm1'})
        CREATE (a)-[:RESULTED_IN]->(:DockingResult {match_id: 'm2'})
        CREATE (b)-[:RESULTED_IN]->(:DockingResult {match_id: 'm1'})
        CREATE (b)-[:RESULTED_IN]->(:DockingResult {match_id: 'm3'})
        CREATE (a)-[:RESULTED_IN]->(:TransactionOutcome {match_id: 'x1'})
        CREATE (b)-[:RESULTED_IN]->(:TransactionOutcome {match_id: 'x1'})
        """,
    )
    scorer = SimilarityScorer(_registry(), graph_driver, DB, domain_spec=orbital_spec)
    assert await scorer._behavioral_similarity("d1", "d2", LABEL) == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_r13_candidate_sharing_only_the_second_comparison_property(graph_driver, clean_db, orbital_spec) -> None:
    """R13: the prefilter admits a candidate sharing any declared comparison value.

    d2 differs on docking_ports (first property) but shares reliability and every
    neighbour: 0.5 * 1/2 + 0.5 * 1.0 = 0.75 >= 0.7, so it is contract-qualified.
    """
    await _write(
        graph_driver,
        """
        CREATE (a:OrbitalDepot {depot_id: 'd1', docking_ports: 6, reliability: 0.9})
        CREATE (b:OrbitalDepot {depot_id: 'd2', docking_ports: 4, reliability: 0.9})
        CREATE (c:OrbitalDepot {depot_id: 'd3', docking_ports: 1, reliability: 0.1})
        CREATE (n:Thing {thing_id: 1}), (a)-[:LINKS]->(n), (b)-[:LINKS]->(n), (c)-[:LINKS]->(n)
        """,
    )
    registry = _registry(
        threshold=0.7,
        property_weight=0.5,
        structural_weight=0.5,
        behavioral_weight=0.0,
        comparison_properties=["docking_ports", "reliability"],
    )
    scorer = SimilarityScorer(registry, graph_driver, DB, domain_spec=orbital_spec)
    found = await scorer.find_candidates("d1", LABEL)
    assert found == [{"entity_id": "d2", "similarity": 0.75}]


@pytest.mark.parametrize("insertion", [["c1", "c2", "c3", "c4"], ["c4", "c3", "c2", "c1"]])
@pytest.mark.asyncio
async def test_r14_capped_prefilter_is_deterministic_across_insertion_order(
    graph_driver, clean_db, orbital_spec, insertion: list[str]
) -> None:
    """R14: more qualified candidates than max_candidates; tied scores; same subset every order."""
    await _write(graph_driver, "CREATE (:OrbitalDepot {depot_id: 'src', docking_ports: 6})")
    for cid in insertion:
        await _write(graph_driver, "CREATE (:OrbitalDepot {depot_id: $id, docking_ports: 6})", {"id": cid})
    registry = _registry(threshold=0.5, property_weight=1.0, structural_weight=0.0, behavioral_weight=0.0)
    scorer = SimilarityScorer(registry, graph_driver, DB, domain_spec=orbital_spec)
    found = await scorer.find_candidates("src", LABEL, limit=2)
    assert found == [{"entity_id": "c1", "similarity": 1.0}, {"entity_id": "c2", "similarity": 1.0}]


# ── N2: link persistence on the declared identity ────────────────────────────


@pytest.mark.parametrize("order", [["d_a", "d_b", "d_hub"], ["d_hub", "d_b", "d_a"]])
@pytest.mark.asyncio
async def test_r08_batch_links_two_duplicates_to_highest_degree_canonical(
    graph_driver, clean_db, orbital_spec, order: list[str]
) -> None:
    """R08 (+R11 rerun, R12 readback): exactly two links to d_hub, rerun creates none."""
    await _write(graph_driver, DUPLICATES_SEED)
    # Give d_hub its extra non-resolution edge after the others are created, so
    # canonical choice depends on degree, never on creation order.
    await _write(graph_driver, "MATCH (h:OrbitalDepot {depot_id: 'd_hub'}) CREATE (h)-[:LINKS]->(:Thing {thing_id: 9})")
    before = await _snapshot(graph_driver)
    resolver = EntityResolver(_registry(threshold=0.6), graph_driver, DB, domain_spec=orbital_spec)

    first = await resolver.resolve_entity(order[0], LABEL)
    rerun = await resolver.resolve_batch(LABEL)
    links = await _links(graph_driver)

    assert links == [("d_a", "d_hub"), ("d_b", "d_hub")]
    assert first["canonical_id"] == "d_hub"
    assert first["merged_count"] == 2
    assert rerun["total_merged"] == 0
    edges = await _read(
        graph_driver,
        """
        MATCH (a:OrbitalDepot)-[r:RESOLVED_FROM]->(b:OrbitalDepot)
        RETURN a.depot_id AS src, r.domain_id AS domain_id, r.confidence AS confidence, r.signal AS signal
        ORDER BY src
        """,
    )
    assert [e["domain_id"] for e in edges] == [DB, DB]
    assert all(0.6 <= e["confidence"] <= 1.0 for e in edges)
    assert {e["signal"] for e in edges} == {"semantic_registry"}
    assert await _snapshot(graph_driver) == before


@pytest.mark.asyncio
async def test_r08_batch_mode_from_clean_graph(graph_driver, clean_db, orbital_spec) -> None:
    """R08: batch mode alone, keyed by depot_id, creates exactly two idempotent links."""
    await _write(graph_driver, DUPLICATES_SEED)
    await _write(graph_driver, "MATCH (h:OrbitalDepot {depot_id: 'd_hub'}) CREATE (h)-[:LINKS]->(:Thing {thing_id: 9})")
    resolver = EntityResolver(_registry(threshold=0.6), graph_driver, DB, domain_spec=orbital_spec)
    first = await resolver.resolve_batch(LABEL)
    second = await resolver.resolve_batch(LABEL)
    assert first == {"total_entities": 3, "total_merged": 2, "resolution_groups": 1}
    assert second["total_merged"] == 0
    assert await _links(graph_driver) == [("d_a", "d_hub"), ("d_b", "d_hub")]


@pytest.mark.asyncio
async def test_r10_resolution_affects_only_the_named_tenant_database(graph_driver, orbital_spec) -> None:
    """R10: identical ids in two tenant databases; resolving one leaves the other unchanged."""
    tenants = ("res-tenant-a", "res-tenant-b")
    for name in tenants:
        await graph_driver.execute_query(
            cypher=f"CREATE DATABASE `{name}` IF NOT EXISTS WAIT", parameters={}, database="system"
        )
        await _write(graph_driver, "MATCH (n) DETACH DELETE n", db=name)
        await _write(graph_driver, DUPLICATES_SEED, db=name)
    try:
        before_b = await _snapshot(graph_driver, db=tenants[1])
        resolver = EntityResolver(_registry(threshold=0.6), graph_driver, tenants[0], domain_spec=orbital_spec)
        result = await resolver.resolve_batch(LABEL)
        assert result["total_merged"] == 2
        assert len(await _links(graph_driver, db=tenants[0])) == 2
        assert await _links(graph_driver, db=tenants[1]) == []
        assert await _snapshot(graph_driver, db=tenants[1]) == before_b
        domains = await _read(
            graph_driver, "MATCH ()-[r:RESOLVED_FROM]->() RETURN DISTINCT r.domain_id AS d", db=tenants[0]
        )
        assert [r["d"] for r in domains] == [tenants[0]]
    finally:
        for name in tenants:
            await _write(graph_driver, "MATCH (n) DETACH DELETE n", db=name)


@pytest.mark.asyncio
async def test_r11_concurrent_identical_requests_create_each_link_once(graph_driver, clean_db, orbital_spec) -> None:
    """R11: parallel identical single and batch requests; stable canonical, two links, counts sum to 2."""
    await _write(graph_driver, DUPLICATES_SEED)
    await _write(graph_driver, "MATCH (h:OrbitalDepot {depot_id: 'd_hub'}) CREATE (h)-[:LINKS]->(:Thing {thing_id: 9})")
    resolvers = [EntityResolver(_registry(threshold=0.6), graph_driver, DB, domain_spec=orbital_spec) for _ in range(6)]
    singles = await asyncio.gather(*(r.resolve_entity("d_a", LABEL) for r in resolvers[:3]))
    batches = await asyncio.gather(*(r.resolve_batch(LABEL) for r in resolvers[3:]))
    edges = await _read(
        graph_driver, "MATCH (a:OrbitalDepot)-[r:RESOLVED_FROM]->(b) RETURN a.depot_id AS s, b.depot_id AS t"
    )
    assert sorted((e["s"], e["t"]) for e in edges) == [("d_a", "d_hub"), ("d_b", "d_hub")]
    assert {s["canonical_id"] for s in singles if s["merged_entity_ids"]} == {"d_hub"}
    created = sum(s["merged_count"] for s in singles) + sum(b["total_merged"] for b in batches)
    assert created == 2


@pytest.mark.asyncio
async def test_x05_resolved_duplicate_remains_a_match_candidate(
    engine_deps, graph_driver, clean_db, orbital_spec
) -> None:
    """X05 (+R12): link-only resolution neither suppresses nor merges match candidates."""
    from engine.handlers import handle_match
    from tests.fixtures.orbital_match import DIRECTION, depot

    await _write(
        graph_driver,
        "UNWIND $batch AS row MERGE (n:OrbitalDepot {depot_id: row.depot_id}) SET n += row",
        {"batch": [depot("d1", docking_ports=6), depot("d2", docking_ports=6), depot("d3", docking_ports=6)]},
        db=ORBITAL,
    )
    query = {"query": {"min_ports": 2}, "match_direction": DIRECTION, "top_n": 10}

    def ids(result: dict[str, Any]) -> list[str]:
        return sorted(c["candidate"]["depot_id"] for c in result.get("candidates", []))

    before_match = ids(await handle_match(ORBITAL, query))
    before_graph = await _snapshot(graph_driver, db=ORBITAL)
    resolver = EntityResolver(
        _registry(
            threshold=0.6,
            property_weight=1.0,
            structural_weight=0.0,
            behavioral_weight=0.0,
            comparison_properties=["docking_ports", "reliability"],
        ),
        graph_driver,
        ORBITAL,
        domain_spec=orbital_spec,
    )
    resolved = await resolver.resolve_batch(LABEL)
    after_match = ids(await handle_match(ORBITAL, query))

    assert resolved["total_merged"] == 2
    assert len(await _links(graph_driver, db=ORBITAL)) == 2
    assert before_match == after_match == ["d1", "d2", "d3"]
    assert await _snapshot(graph_driver, db=ORBITAL) == before_graph
