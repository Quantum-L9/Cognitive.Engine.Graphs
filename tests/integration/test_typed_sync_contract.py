"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, sync, contract, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_typed_sync_contract.py
The typed sync contract against a real Neo4j, for PlasticOS and for the
synthetic ``orbital-logistics`` pack (vocabulary no shipped pack shares):

(a) a ``list<map>`` childsync writes one child + edge per element, and a
    ``managedby: static`` child is never overwritten;
(b) a JSON-encoded string or a list of strings for a ``list<map>`` field is
    refused with precise defects and nothing is written;
(c) a ``list<scalar>`` taxonomy field creates one edge per element;
(d) a declared float sent as ``"high"`` is refused, nothing written;
(e) an undeclared key is dropped (flag off) or refused (flag on).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import ValidationError, handle_sync, init_dependencies
from engine.state import get_state
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

ORBITAL = "orbital-logistics"
PLASTICOS = "plasticos"


async def _rows(driver: GraphDriver, cypher: str, database: str) -> list[dict[str, Any]]:
    return await driver.execute_query(cypher, database=database)


async def _count(driver: GraphDriver, cypher: str, database: str) -> int:
    [row] = await driver.execute_query(cypher, database=database)
    return int(row["n"])


async def _refused(tenant: str, entity_type: str, batch: list[Any]) -> str:
    with pytest.raises(ValidationError) as refused:
        await handle_sync(tenant, {"entity_type": entity_type, "batch": batch})
    return str(refused.value)


# ── PlasticOS design pack (pack-specific) ─────────────────────────────────────
# These pin the plasticos pack's own sync declarations (its childsync, taxonomy
# and static nodes), so they load tests/fixtures/design_packs explicitly; the
# orbital-logistics section below proves the same contract on the core.


@pytest_asyncio.fixture
async def plasticos(design_pack_deps: None, graph_driver: GraphDriver) -> GraphDriver:
    return graph_driver


async def test_plasticos_list_of_maps_childsync_writes_children_and_never_overwrites_static_nodes(
    plasticos: GraphDriver,
) -> None:
    await handle_sync(
        PLASTICOS,
        {"entity_type": "polymerfamilies", "batch": [{"code": "PET", "name": "Polyethylene terephthalate"}]},
    )
    result = await handle_sync(
        PLASTICOS,
        {
            "entity_type": "facilities",
            "batch": [
                {
                    "facility_id": "tf-1",
                    "name": "Typed Facility",
                    "rate_per_ton": 120,
                    "processed_polymers": [{"code": "PET", "name": "OVERWRITTEN"}, {"code": "HDPE"}],
                }
            ],
        },
    )
    assert result["synced_count"] == 1
    edges = await _rows(
        plasticos,
        "MATCH (:Facility {facility_id: 'tf-1'})-[:PROCESSES]->(p:PolymerFamily) "
        "RETURN p.code AS code, p.name AS name ORDER BY code",
        PLASTICOS,
    )
    # One PROCESSES edge per element; the existing static PET keeps its name and
    # the item's undeclared `name` never reaches PolymerFamily.
    assert edges == [{"code": "HDPE", "name": None}, {"code": "PET", "name": "Polyethylene terephthalate"}]
    [facility] = await _rows(
        plasticos, "MATCH (f:Facility {facility_id: 'tf-1'}) RETURN properties(f) AS props", PLASTICOS
    )
    assert "processed_polymers" not in facility["props"]
    assert facility["props"]["rate_per_ton"] == 120


async def test_plasticos_json_string_and_list_of_strings_are_refused_and_nothing_is_written(
    plasticos: GraphDriver,
) -> None:
    message = await _refused(
        PLASTICOS,
        "facilities",
        [
            {"facility_id": "tf-2", "processed_polymers": json.dumps([{"code": "PET"}])},
            {"facility_id": "tf-3", "processed_polymers": ["PET", "PP"]},
        ],
    )
    assert "row 0 processed_polymers: expected list<map>, got string" in message
    assert "row 1 processed_polymers[0]: expected map, got string" in message
    assert "row 1 processed_polymers[1]: expected map, got string" in message
    assert await _count(plasticos, "MATCH (n) RETURN count(n) AS n", PLASTICOS) == 0


async def test_plasticos_scalar_taxonomy_field_links_one_node(plasticos: GraphDriver) -> None:
    await handle_sync(PLASTICOS, {"entity_type": "polymerfamilies", "batch": [{"code": "PP"}]})
    await handle_sync(
        PLASTICOS,
        {
            "entity_type": "supply-opportunities",
            "batch": [{"opportunity_id": "so-1", "facility_id": "tf-4", "polymer_type": "PP"}],
        },
    )
    assert (
        await _count(
            plasticos,
            "MATCH (:SupplyOpportunity {opportunity_id: 'so-1'})-[:FULFILLS_POLYMER]->(:PolymerFamily {code: 'PP'}) "
            "RETURN count(*) AS n",
            PLASTICOS,
        )
        == 1
    )


async def test_plasticos_declared_float_sent_as_text_is_refused(plasticos: GraphDriver) -> None:
    message = await _refused(PLASTICOS, "facilities", [{"facility_id": "tf-5", "rate_per_ton": "high"}])
    assert "row 0 rate_per_ton: expected float, got string" in message
    assert await _count(plasticos, "MATCH (n) RETURN count(n) AS n", PLASTICOS) == 0


async def test_plasticos_undeclared_key_is_dropped_or_refused(
    plasticos: GraphDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await handle_sync(
        PLASTICOS, {"entity_type": "facilities", "batch": [{"facility_id": "tf-6", "colour": "teal"}]}
    )
    assert result["undeclared_properties"] == {"colour": 1}
    [facility] = await _rows(
        plasticos, "MATCH (f:Facility {facility_id: 'tf-6'}) RETURN properties(f) AS props", PLASTICOS
    )
    assert "colour" not in facility["props"]

    monkeypatch.setattr(settings, "sync_undeclared_property_refusal_enabled", True)
    message = await _refused(PLASTICOS, "facilities", [{"facility_id": "tf-7", "colour": "teal"}])
    assert "row 0 colour: expected no such declared property, got string" in message
    assert await _count(plasticos, "MATCH (f:Facility {facility_id: 'tf-7'}) RETURN count(f) AS n", PLASTICOS) == 0


# ── orbital-logistics (synthetic, non-PlasticOS) ──────────────────────────────


async def _wipe(driver: GraphDriver) -> None:
    await driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=ORBITAL)


@pytest_asyncio.fixture
async def orbital(graph_driver: GraphDriver) -> AsyncGenerator[GraphDriver, None]:
    await graph_driver.execute_query(f"CREATE DATABASE `{ORBITAL}` IF NOT EXISTS WAIT", database="system")
    await _wipe(graph_driver)
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(SYNTHETIC_DOMAINS_ROOT)))
    await handle_sync(
        ORBITAL,
        {
            "entity_type": "orbit-classes",
            "batch": [
                {"class_code": "LEO", "altitude_km": 400.0},
                {"class_code": "MEO", "altitude_km": 20000.0},
                {"class_code": "GEO", "altitude_km": 35786.0},
            ],
        },
    )
    yield graph_driver
    await _wipe(graph_driver)
    get_state().reset()


async def test_orbital_typed_row_writes_list_property_children_and_one_edge_per_taxonomy_element(
    orbital: GraphDriver,
) -> None:
    result = await handle_sync(
        ORBITAL,
        {
            "entity_type": "orbital-depots",
            "batch": [
                {
                    "depot_id": "dep-1",
                    "reliability": 0.97,
                    "certifications": ["iso-9001", "ccsds"],
                    "served_orbits": ["LEO", "MEO", "GEO"],
                    "stocked_grades": [{"grade_code": "LOX", "purity": 0.995}, {"grade_code": "RP1"}],
                },
                {"depot_id": "dep-2", "served_orbits": ["GEO"], "stocked_grades": []},
            ],
        },
    )
    assert result["synced_count"] == 2
    [depot] = await _rows(orbital, "MATCH (d:OrbitalDepot {depot_id: 'dep-1'}) RETURN properties(d) AS p", ORBITAL)
    assert depot["p"]["certifications"] == ["iso-9001", "ccsds"]
    assert depot["p"]["served_orbits"] == ["LEO", "MEO", "GEO"]
    assert "stocked_grades" not in depot["p"]
    orbits = await _rows(
        orbital,
        "MATCH (d:OrbitalDepot)-[:SERVES_ORBIT]->(o:OrbitClass) RETURN d.depot_id AS depot, o.class_code AS orbit "
        "ORDER BY depot, orbit",
        ORBITAL,
    )
    assert orbits == [
        {"depot": "dep-1", "orbit": "GEO"},
        {"depot": "dep-1", "orbit": "LEO"},
        {"depot": "dep-1", "orbit": "MEO"},
        {"depot": "dep-2", "orbit": "GEO"},
    ]
    grades = await _rows(
        orbital,
        "MATCH (:OrbitalDepot {depot_id: 'dep-1'})-[:STOCKS]->(g:PropellantGrade) "
        "RETURN g.grade_code AS code, g.purity AS purity ORDER BY code",
        ORBITAL,
    )
    assert grades == [{"code": "LOX", "purity": 0.995}, {"code": "RP1", "purity": None}]


async def test_orbital_json_string_and_list_of_strings_are_refused_and_nothing_is_written(
    orbital: GraphDriver,
) -> None:
    message = await _refused(
        ORBITAL,
        "orbital-depots",
        [
            {"depot_id": "dep-3", "stocked_grades": json.dumps([{"grade_code": "LOX"}])},
            {"depot_id": "dep-4", "stocked_grades": ["LOX"], "certifications": "iso-9001"},
        ],
    )
    assert "row 0 stocked_grades: expected list<map>, got string" in message
    assert "row 1 stocked_grades[0]: expected map, got string" in message
    assert "row 1 certifications: expected list<string>, got string" in message
    assert await _count(orbital, "MATCH (n:OrbitalDepot) RETURN count(n) AS n", ORBITAL) == 0
    assert await _count(orbital, "MATCH (n:PropellantGrade) RETURN count(n) AS n", ORBITAL) == 0


async def test_orbital_declared_float_sent_as_text_is_refused(orbital: GraphDriver) -> None:
    message = await _refused(ORBITAL, "orbital-depots", [{"depot_id": "dep-5", "reliability": "high"}])
    assert "row 0 reliability: expected float, got string" in message
    assert await _count(orbital, "MATCH (n:OrbitalDepot) RETURN count(n) AS n", ORBITAL) == 0


async def test_orbital_undeclared_key_is_dropped_or_refused(
    orbital: GraphDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await handle_sync(
        ORBITAL,
        {
            "entity_type": "orbital-depots",
            "batch": [{"depot_id": "dep-6", "colour": "teal", "stocked_grades": [{"grade_code": "N2O4", "lot": 7}]}],
        },
    )
    assert result["undeclared_properties"] == {"colour": 1, "stocked_grades[].lot": 1}
    [depot] = await _rows(orbital, "MATCH (d:OrbitalDepot {depot_id: 'dep-6'}) RETURN properties(d) AS p", ORBITAL)
    assert "colour" not in depot["p"]
    [grade] = await _rows(orbital, "MATCH (g:PropellantGrade {grade_code: 'N2O4'}) RETURN properties(g) AS p", ORBITAL)
    assert "lot" not in grade["p"]

    monkeypatch.setattr(settings, "sync_undeclared_property_refusal_enabled", True)
    message = await _refused(ORBITAL, "orbital-depots", [{"depot_id": "dep-7", "colour": "teal"}])
    assert "row 0 colour: expected no such declared property, got string" in message
    assert await _count(orbital, "MATCH (d:OrbitalDepot {depot_id: 'dep-7'}) RETURN count(d) AS n", ORBITAL) == 0
