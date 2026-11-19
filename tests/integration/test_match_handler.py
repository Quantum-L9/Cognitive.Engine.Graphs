"""Integration tests — match handler: full pipeline, top_n, invalid payload.

Real payload contract (engine/handlers.py::handle_match):
    query: dict (required — resolved against domain queryschema)
    match_direction: str (required — must exist in matchentities)
    top_n: int (optional, clamped to [1, 1000])
    weights: dict (optional, validated to [0, 1], sum <= 1.0)

Core behaviour runs on the synthetic orbital-logistics pack (ADR-112 Stage 4,
A16): candidate=OrbitalDepot for direction 'window_to_depot', gated by the
pack's hazmat and port gates, seeded in the 'orbital-logistics' database. The
multi-path test adds a SERVES_ORBIT traversal gate (tests/fixtures/orbital_match).

The plasticos and freight-matching tests below guard those design packs' own
gate declarations (pack regressions, not core behaviour), so they load
tests/fixtures/design_packs explicitly through ``design_pack_deps``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.fixtures.orbital_match import DIRECTION, ORBITAL, depot, orbit_gated, serve

pytestmark = pytest.mark.integration

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:OrbitalDepot {depot_id: row.depot_id})
SET n += row
SET n.synced_at = datetime()
"""


def _depot_ids(result: dict) -> list[str]:
    return [c["candidate"]["depot_id"] for c in result.get("candidates", [])]


@pytest.mark.asyncio
async def test_match_returns_candidates(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"batch": [depot("d1", docking_ports=6), depot("d2", lat=34.10, lon=-118.30)]},
        database=ORBITAL,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        ORBITAL,
        {"query": {"min_ports": 2}, "match_direction": DIRECTION, "top_n": 10},
    )
    assert "candidates" in result
    assert sorted(_depot_ids(result)) == ["d1", "d2"]


@pytest.mark.asyncio
async def test_match_respects_top_n(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"batch": [depot(f"d{i}") for i in range(5)]},
        database=ORBITAL,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        ORBITAL,
        {"query": {"min_ports": 1}, "match_direction": DIRECTION, "top_n": 2},
    )
    candidates = result.get("candidates", [])
    assert len(candidates) <= 2


@pytest.mark.asyncio
async def test_match_unknown_direction_raises(engine_deps):
    """A match_direction with no candidate entity must raise ValidationError."""
    from engine.handlers import ValidationError, handle_match

    with pytest.raises(ValidationError, match="No candidate entity"):
        await handle_match(
            ORBITAL,
            {"query": {"min_ports": 1}, "match_direction": "nonexistent_direction", "top_n": 5},
        )


MULTI_PATH_SEED_CYPHER = """
MERGE (n:OrbitalDepot {depot_id: $depot_id})
SET n += $props
WITH n
UNWIND $codes AS code
MERGE (o:OrbitClass {class_code: code})
MERGE (n)-[:SERVES_ORBIT]->(o)
"""


@pytest.mark.asyncio
async def test_match_returns_multi_path_candidate_once(engine_deps, graph_driver, domain_spec, clean_db):
    """RU-036 / CEG-COR-007 (RG-048): a depot with 3 SERVES_ORBIT edges is one candidate.

    Before the per-candidate collapse a traversal produced one row per edge and
    the candidate appeared three times, consuming top_n slots.
    """
    for depot_id, codes in (("d_multi", ["LEO", "MEO", "GEO"]), ("d_single", ["LEO"])):
        await graph_driver.execute_write(
            cypher=MULTI_PATH_SEED_CYPHER,
            parameters={"depot_id": depot_id, "props": depot(depot_id), "codes": codes},
            database=ORBITAL,
        )
    serve(graph_driver, orbit_gated(domain_spec))
    from engine.handlers import handle_match

    result = await handle_match(
        ORBITAL,
        {"query": {"orbit_class": "LEO", "min_ports": 1}, "match_direction": DIRECTION, "top_n": 10},
    )
    ids = _depot_ids(result)
    assert ids.count("d_multi") == 1
    assert sorted(ids) == ["d_multi", "d_single"]
    assert result["total_candidates"] == len(set(ids))


# ── plasticos design pack (pack-specific) ─────────────────────────────────────
# These guard the plasticos pack's own gate declarations (RU-033/034/040 were
# pack fixes), so they stay on that pack.

PLASTICOS = "plasticos"
SEED_FACILITY_CYPHER = """
UNWIND $batch AS row
MERGE (n:Facility {entity_id: row.entity_id})
SET n += row
WITH n
MERGE (p:PolymerFamily {code: 'PET'})
MERGE (n)-[rel:PROCESSES {line_id: row.entity_id}]->(p)
SET rel.direction = 'in', rel.form = 'PELLET', rel.line_id = row.entity_id
"""


def _facility(entity_id: str, **overrides) -> dict:
    row = {
        "entity_id": entity_id,
        "name": f"Facility {entity_id}",
        "contamination_tolerance": 0.10,
        "latitude": 34.05,
        "longitude": -118.24,
        "food_grade_certified": False,
        "reinforcement_score": 0.5,
    }
    row.update(overrides)
    return row


def _candidate_ids(result: dict) -> list[str]:
    return [c["candidate"]["entity_id"] for c in result.get("candidates", [])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requires_food_grade", "expected"),
    [(False, {"certified", "uncertified"}), (None, {"certified", "uncertified"}), (True, {"certified"})],
)
async def test_food_grade_requirement_is_an_implication(design_pack_deps, graph_driver, requires_food_grade, expected):
    """RG-045 (RU-034 / CEG-COR-005): not requiring food grade must not exclude certified facilities."""
    await graph_driver.execute_write(
        cypher=SEED_FACILITY_CYPHER,
        parameters={
            "batch": [
                _facility("certified", food_grade_certified=True),
                _facility("uncertified", food_grade_certified=False),
            ]
        },
        database=PLASTICOS,
    )
    from engine.handlers import handle_match

    query = {"polymer_type": "PET", "contamination_pct": 0.04}
    if requires_food_grade is not None:
        query["requires_food_grade"] = requires_food_grade
    result = await handle_match(
        PLASTICOS,
        {"query": query, "match_direction": "supply_opportunity_to_buyer_facility", "top_n": 10},
    )
    assert set(_candidate_ids(result)) == expected


MULTI_POLYMER_SEED = """
UNWIND $batch AS row
MERGE (n:Facility {entity_id: row.entity_id})
SET n += row{.*, polymers: null}
WITH n, row
UNWIND row.polymers AS code
MERGE (p:PolymerFamily {code: code})
MERGE (n)-[rel:PROCESSES {line_id: row.entity_id + '-' + code}]->(p)
SET rel.direction = 'in', rel.form = 'PELLET', rel.line_id = row.entity_id + '-' + code
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(("polymer_type", "expected"), [("PET", ["multi"]), ("HDPE", ["multi", "hdpe_only"])])
async def test_match_returns_only_compatible_facilities_once(design_pack_deps, graph_driver, polymer_type, expected):
    """RG-053 (RU-040 / CEG-COR-013): one row per compatible facility, none for incompatible ones."""
    await graph_driver.execute_write(
        cypher=MULTI_POLYMER_SEED,
        parameters={
            "batch": [
                {**_facility("multi"), "polymers": ["PET", "HDPE", "PP"]},
                {**_facility("hdpe_only"), "polymers": ["HDPE"]},
                {**_facility("ldpe_only"), "polymers": ["LDPE"]},
            ]
        },
        database=PLASTICOS,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        PLASTICOS,
        {
            "query": {"polymer_type": polymer_type, "contamination_pct": 0.04},
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 10,
        },
    )
    ids = _candidate_ids(result)
    assert sorted(ids) == sorted(expected)
    assert len(ids) == len(set(ids))


SUPPLY_SEED = """
UNWIND $batch AS row
MERGE (s:SupplyOpportunity {entity_id: row.entity_id})
SET s += row
WITH s
MERGE (p:PolymerFamily {code: s.polymer_type})
MERGE (s)-[:FULFILLS_POLYMER]->(p)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requires_active_supply", "expected"),
    [(None, {"active"}), (True, {"active"}), (False, {"active", "inactive"})],
)
async def test_active_supply_requirement_is_an_implication_with_a_true_default(
    design_pack_deps, graph_driver, requires_active_supply, expected
):
    """RG-043 (RU-033 / CEG-COR-004): omitted/true -> active only; false -> active and inactive.

    Supply with no `active` value is not a SupplyOpportunity record and never passes.
    """
    await graph_driver.execute_write(
        cypher=SUPPLY_SEED,
        parameters={
            "batch": [
                {"entity_id": "active", "active": True, "available_tons": 50.0, "polymer_type": "PET"},
                {"entity_id": "inactive", "active": False, "available_tons": 50.0, "polymer_type": "PET"},
                {"entity_id": "unknown", "available_tons": 50.0, "polymer_type": "PET"},
            ]
        },
        database=PLASTICOS,
    )
    from engine.handlers import handle_match

    query: dict[str, object] = {"polymer_type": "PET", "volume_tons": 10.0}
    if requires_active_supply is not None:
        query["requires_active_supply"] = requires_active_supply
    result = await handle_match(
        PLASTICOS,
        {"query": query, "match_direction": "buyer_demand_to_supply_opportunity", "top_n": 10},
    )
    assert set(_candidate_ids(result)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required", "eligible", "expected"),
    [
        (None, True, True),
        (False, False, True),
        (False, True, True),
        (True, True, True),
        (True, False, False),
        (True, None, True),
    ],
)
async def test_va_eligibility_fragment_orders_booleans_false_before_true(graph_driver, required, eligible, expected):
    """RU-035 / CEG-COR-005: pins the Cypher boolean ordering the mortgage implication relies on.

    The compiled fragment is evaluated directly rather than through handle_match,
    so the ordering is pinned whether or not the owner declares the pack dormant
    (DEC-15; served by default, owner decision 2026-10-29).
    """
    rows = await graph_driver.execute_query(
        cypher=(
            "WITH $required AS requiresvaeligibility "
            "RETURN (requiresvaeligibility IS NULL OR $vaeligible IS NULL OR "
            "(requiresvaeligibility <= $vaeligible)) AS passes"
        ),
        parameters={"required": required, "vaeligible": eligible},
        database="neo4j",
    )
    assert rows[0]["passes"] is expected


# freight-matching design pack (pack-specific): RU-046 / RU-059 were fixes to
# that pack's date and equipment gate declarations.
FREIGHT_DB = "freight-matching"
CARRIER_SEED = """
UNWIND $batch AS row
MERGE (c:Carrier {carrierid: row.carrierid})
SET c += row
"""


@pytest_asyncio.fixture
async def freight_db(graph_driver):
    """The per-domain freight-matching database, emptied after the test."""
    await graph_driver.execute_query(
        cypher=f"CREATE DATABASE `{FREIGHT_DB}` IF NOT EXISTS WAIT", parameters={}, database="system"
    )
    yield
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=FREIGHT_DB)


def _freight_query(**overrides: object) -> dict[str, object]:
    query: dict[str, object] = {
        "loadid": "L1",
        "pickuplat": 41.8,
        "pickuplon": -87.6,
        "deliverylat": 39.7,
        "deliverylon": -104.9,
        "weightlbs": 30000.0,
        "equipmenttype": "van",
        "pickupdate": "2026-11-02T08:00:00Z",
        "deliverydate": "2026-11-04T08:00:00Z",
        "ratepermile": 2.5,
    }
    query.update(overrides)
    return query


def _carrier_ids(result: dict) -> set[str]:
    return {c["candidate"]["carrierid"] for c in result.get("candidates", [])}


@pytest.mark.asyncio
async def test_freight_carrier_available_before_pickup_qualifies(design_pack_deps, graph_driver, freight_db):
    """RU-046 / CEG-COR-069: availabledate <= pickupdate (same ISO-8601 string form as the query)."""
    await graph_driver.execute_write(
        cypher=CARRIER_SEED,
        parameters={
            "batch": [
                {"carrierid": "day_before", "availabledate": "2026-11-01T08:00:00Z", "equipmenttypes": "van"},
                {"carrierid": "same_time", "availabledate": "2026-11-02T08:00:00Z", "equipmenttypes": "van"},
                {"carrierid": "day_after", "availabledate": "2026-11-03T08:00:00Z", "equipmenttypes": "van"},
            ]
        },
        database=FREIGHT_DB,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        FREIGHT_DB, {"query": _freight_query(), "match_direction": "loadtocarrier", "top_n": 10}
    )
    assert _carrier_ids(result) == {"day_before", "same_time"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("equipmenttype", "expected"), [("van", {"van_carrier"}), ("reefer", {"reefer_carrier"})])
async def test_freight_equipment_match_is_scalar_equality(
    design_pack_deps, graph_driver, freight_db, equipmenttype, expected
):
    """RG-074 (RU-059 / CEG-COR-164): equipmenttypes='van' matches 'van' and not 'reefer'."""
    await graph_driver.execute_write(
        cypher=CARRIER_SEED,
        parameters={
            "batch": [
                {"carrierid": "van_carrier", "equipmenttypes": "van"},
                {"carrierid": "reefer_carrier", "equipmenttypes": "reefer"},
            ]
        },
        database=FREIGHT_DB,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        FREIGHT_DB,
        {"query": _freight_query(equipmenttype=equipmenttype), "match_direction": "loadtocarrier", "top_n": 10},
    )
    assert _carrier_ids(result) == expected


@pytest.mark.asyncio
async def test_scalar_membership_and_equality_forms_on_neo4j(graph_driver):
    """RG-074 evidence: the old `prop IN $scalar` form never matches as specified; `=` does."""
    rows = await graph_driver.execute_query(
        cypher="RETURN 'van' = $equipmenttype AS equal", parameters={"equipmenttype": "van"}, database="neo4j"
    )
    assert rows[0]["equal"] is True
    from neo4j.exceptions import ClientError

    try:
        rows = await graph_driver.execute_query(
            cypher="RETURN 'van' IN $equipmenttype AS member", parameters={"equipmenttype": "van"}, database="neo4j"
        )
    except ClientError:
        return  # Neo4j rejects IN over a non-list operand outright (type error)
    assert rows[0]["member"] is not True
