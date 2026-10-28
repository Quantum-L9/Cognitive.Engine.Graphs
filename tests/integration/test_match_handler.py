"""Integration tests — match handler: full pipeline, top_n, invalid payload.

Real payload contract (engine/handlers.py::handle_match):
    query: dict (required — resolved against domain queryschema)
    match_direction: str (required — must exist in matchentities)
    top_n: int (optional, clamped to [1, 1000])
    weights: dict (optional, validated to [0, 1], sum <= 1.0)

The plasticos spec declares candidate=Facility for direction
'supply_opportunity_to_buyer_facility' and a traversal gate requiring
(candidate)-[:PROCESSES]->(:PolymerFamily {code: $polymer_type}), so seed data
must live in the 'plasticos' database and include that edge.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:Facility {entity_id: row.entity_id})
SET n += row
SET n.synced_at = datetime()
WITH n
MERGE (p:PolymerFamily {code: 'PET'})
MERGE (n)-[:PROCESSES]->(p)
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


@pytest.mark.asyncio
async def test_match_returns_candidates(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={
            "batch": [
                _facility("f1", name="Alpha Recycle", contamination_tolerance=0.05),
                _facility("f2", name="Beta Process", contamination_tolerance=0.10, latitude=34.10, longitude=-118.30),
            ]
        },
        database="plasticos",
    )
    from engine.handlers import handle_match

    result = await handle_match(
        "plasticos",
        {
            "query": {
                "polymer_type": "PET",
                "contamination_pct": 0.04,
                "lat": 34.05,
                "lon": -118.24,
            },
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 10,
        },
    )
    assert "candidates" in result
    assert result.get("status") in ("ok", "success", None) or "candidates" in result


@pytest.mark.asyncio
async def test_match_respects_top_n(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"batch": [_facility(f"f{i}", contamination_tolerance=0.20) for i in range(5)]},
        database="plasticos",
    )
    from engine.handlers import handle_match

    result = await handle_match(
        "plasticos",
        {
            "query": {"polymer_type": "PET", "contamination_pct": 0.01},
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 2,
        },
    )
    candidates = result.get("candidates", [])
    assert len(candidates) <= 2


@pytest.mark.asyncio
async def test_match_unknown_direction_raises(engine_deps):
    """A match_direction with no candidate entity must raise ValidationError."""
    from engine.handlers import ValidationError, handle_match

    with pytest.raises(ValidationError, match="No candidate entity"):
        await handle_match(
            "plasticos",
            {"query": {"polymer_type": "PET"}, "match_direction": "nonexistent_direction", "top_n": 5},
        )


MULTI_PATH_SEED_CYPHER = """
MERGE (n:Facility {entity_id: $entity_id})
SET n += $props
WITH n
UNWIND $codes AS code
MERGE (p:PolymerFamily {code: code})
MERGE (n)-[:PROCESSES]->(p)
"""


@pytest.mark.asyncio
async def test_match_returns_multi_path_candidate_once(engine_deps, graph_driver, clean_db):
    """RU-036 / CEG-COR-007 (RG-048): a Facility with 3 PROCESSES edges is one candidate.

    Before the per-candidate collapse the required traversal step produced one
    row per edge and the facility appeared three times, consuming top_n slots.
    """
    await graph_driver.execute_write(
        cypher=MULTI_PATH_SEED_CYPHER,
        parameters={
            "entity_id": "f_multi",
            "props": _facility("f_multi", contamination_tolerance=0.10),
            "codes": ["PET", "HDPE", "PP"],
        },
        database="plasticos",
    )
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"batch": [_facility("f_single", contamination_tolerance=0.10)]},
        database="plasticos",
    )
    from engine.handlers import handle_match

    result = await handle_match(
        "plasticos",
        {
            "query": {"polymer_type": "PET", "contamination_pct": 0.05},
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 10,
        },
    )
    ids = [c["candidate"].get("entity_id") for c in result["candidates"]]
    assert ids.count("f_multi") == 1
    assert sorted(ids) == ["f_multi", "f_single"]
    assert result["total_candidates"] == len(set(ids))


def _candidate_ids(result: dict) -> list[str]:
    return [c["candidate"]["entity_id"] for c in result.get("candidates", [])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requires_food_grade", "expected"),
    [(False, {"certified", "uncertified"}), (None, {"certified", "uncertified"}), (True, {"certified"})],
)
async def test_food_grade_requirement_is_an_implication(
    engine_deps, graph_driver, clean_db, requires_food_grade, expected
):
    """RG-045 (RU-034 / CEG-COR-005): not requiring food grade must not exclude certified facilities."""
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={
            "batch": [
                _facility("certified", food_grade_certified=True),
                _facility("uncertified", food_grade_certified=False),
            ]
        },
        database="plasticos",
    )
    from engine.handlers import handle_match

    query = {"polymer_type": "PET", "contamination_pct": 0.04}
    if requires_food_grade is not None:
        query["requires_food_grade"] = requires_food_grade
    result = await handle_match(
        "plasticos",
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
MERGE (n)-[:PROCESSES]->(p)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(("polymer_type", "expected"), [("PET", ["multi"]), ("HDPE", ["multi", "hdpe_only"])])
async def test_match_returns_only_compatible_facilities_once(
    engine_deps, graph_driver, clean_db, polymer_type, expected
):
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
        database="plasticos",
    )
    from engine.handlers import handle_match

    result = await handle_match(
        "plasticos",
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
    engine_deps, graph_driver, clean_db, requires_active_supply, expected
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
        database="plasticos",
    )
    from engine.handlers import handle_match

    query: dict[str, object] = {"polymer_type": "PET", "volume_tons": 10.0}
    if requires_active_supply is not None:
        query["requires_active_supply"] = requires_active_supply
    result = await handle_match(
        "plasticos",
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

    The mortgage pack is dormant (DEC-15), so the compiled fragment is evaluated
    directly rather than through handle_match.
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
async def test_freight_carrier_available_before_pickup_qualifies(engine_deps, graph_driver, freight_db):
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
