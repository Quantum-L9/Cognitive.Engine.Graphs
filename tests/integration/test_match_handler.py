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

import math

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
WITH n, row
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


# G03-MATCH-SCORING. point.distance on this Neo4j uses spherical radius 6_378_140 m:
# one degree of latitude is that radius times pi/180. The formulas below are the
# dimension definitions, not the assembler's text.

_EARTH_RADIUS_M = 6_378_140.0
_BUYER = "supply_opportunity_to_buyer_facility"
_SUPPLY = "buyer_demand_to_supply_opportunity"
_MATCH_TONS = 18.0
_OPPORTUNITY_TONS = 22.0
_LOAD_LBS = 40000
_BUYER_WEIGHTS = {
    "geo_proximity": 0.25,
    "credit_score": 0.15,
    "contamination_tolerance_score": 0.1,
    "community_match": 0.1,
    "rate_score": 0.15,
    "price_alignment": 0.15,
    "recency": 0.1,
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radians = math.pi / 180.0
    dlat = (lat2 - lat1) * radians
    dlon = (lon2 - lon1) * radians
    chord = math.sin(dlat / 2.0) ** 2 + math.cos(lat1 * radians) * math.cos(lat2 * radians) * math.sin(dlon / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(chord))


def _clamp(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _log_norm(value: float, maximum: float) -> float:
    return _clamp(math.log(1.0 + value) / math.log(1.0 + maximum))


def _buyer_dims(
    *,
    lat: float,
    credit: float,
    contamination: float,
    community: int,
    rate: float,
    dated: bool,
) -> dict[str, float]:
    distance = _haversine_m(40.0, -75.0, lat, -75.0)
    community_score = 0.92 if community == 7 else 0.2
    price = 1.0 - abs(math.log(rate / 1000.0)) / 2.0 if rate > 0 else 0.0
    # SC-001: the declared-signal temporalproximity compiler is the default; a
    # facility dated now scores the pure decay 1.0 (the legacy mix capped it at 0.6).
    recency = 1.0 if dated else 0.0
    return {
        "geo_proximity": _clamp(1.0 / (1.0 + distance / 800_000.0)),
        "credit_score": _log_norm(credit, 100.0) if credit > -1.0 else 0.0,
        "contamination_tolerance_score": _clamp(1.0 - contamination / 100.0),
        "community_match": community_score,
        "rate_score": _log_norm(rate, 1000.0) if rate > -1.0 else 0.0,
        "price_alignment": _clamp(price),
        "recency": recency,
    }


def _weighted(dims: dict[str, float]) -> float:
    return sum(_BUYER_WEIGHTS[name] * value for name, value in dims.items())


_FACILITY_SEED = """
UNWIND $batch AS row
MERGE (n:Facility {facility_id: row.facility_id})
SET n += row.props
SET n.last_transaction_date = CASE WHEN row.stamp_now THEN datetime() ELSE null END
WITH n, row
UNWIND row.lines AS line
MERGE (p:PolymerFamily {code: line.code})
MERGE (n)-[rel:PROCESSES {line_id: line.line_id}]->(p)
SET rel.line_id = line.line_id, rel.direction = line.direction, rel.form = line.form
"""


def _line(facility_id: str, direction: str, form: str | None, code: str = "HDPE") -> dict[str, object]:
    line: dict[str, object] = {"line_id": f"{facility_id}-line", "code": code, "direction": direction}
    if form is not None:
        line["form"] = form
    return line


def _props(lat: float = 40.0, **overrides: object) -> dict[str, object]:
    props: dict[str, object] = {
        "name": "Buyer",
        "lat": lat,
        "lon": -75.0,
        "credit_score": 100.0,
        "min_density": 0.9,
        "max_density": 1.0,
        "min_mfi": 2.0,
        "max_mfi": 12.0,
        "contamination_tolerance": 0.0,
        "food_grade_certified": False,
        "capacity_tons_month": 40.0,
        "community_id": 7,
        "rate_per_ton": 1000.0,
    }
    props.update(overrides)
    return props


@pytest.mark.asyncio
async def test_plasticos_buyer_scores_match_independent_dimensions(design_pack_deps, graph_driver):
    """Every active buyer dimension, plus a failed hard gate that a high score cannot restore."""
    batch = [
        {
            "facility_id": "facility:fit",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:fit", "in", "PALLETS")],
        },
        {
            "facility_id": "facility:offset",
            "stamp_now": True,
            "props": _props(lat=41.0),
            "lines": [_line("facility:offset", "in", "TOTERS")],
        },
        {
            "facility_id": "facility:null-capacity",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:null-capacity", "in", "PALLETS")],
        },
        {
            "facility_id": "facility:outlier",
            "stamp_now": False,
            "props": _props(credit_score=-5.0, community_id=3, rate_per_ton=1_000_000.0),
            "lines": [_line("facility:outlier", "in", "BALES")],
        },
        {
            "facility_id": "facility:low-capacity",
            "stamp_now": True,
            "props": _props(capacity_tons_month=10.0),
            "lines": [_line("facility:low-capacity", "in", "PALLETS")],
        },
        {
            "facility_id": "facility:null-contamination",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:null-contamination", "in", "PALLETS")],
        },
        {
            "facility_id": "facility:out-only",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:out-only", "out", "REGRIND")],
        },
        {
            "facility_id": "facility:blank-form",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:blank-form", "in", "")],
        },
        {
            "facility_id": "facility:no-form",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:no-form", "in", None)],
        },
        {
            "facility_id": "facility:density-fail",
            "stamp_now": True,
            "props": _props(max_density=0.2),
            "lines": [_line("facility:density-fail", "in", "PALLETS")],
        },
        {
            "facility_id": "facility:wrong-polymer",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:wrong-polymer", "in", "PALLETS", code="PP")],
        },
        {
            "facility_id": "facility:pvc-only",
            "stamp_now": True,
            "props": _props(),
            "lines": [_line("facility:pvc-only", "in", "PALLETS", code="PVC")],
        },
        {
            "facility_id": "facility:intolerant",
            "stamp_now": True,
            "props": _props(contamination_tolerance=-1.0),
            "lines": [_line("facility:intolerant", "in", "PALLETS")],
        },
    ]
    batch[2]["props"].pop("capacity_tons_month")
    batch[5]["props"].pop("contamination_tolerance")
    await graph_driver.execute_write(cypher=_FACILITY_SEED, parameters={"batch": batch}, database=PLASTICOS)

    from engine.handlers import handle_match

    query = {
        "polymer_type": "HDPE",
        "density": 0.95,
        "mfi": 8.0,
        "contamination_pct": 0.0,
        "requires_food_grade": False,
        "volume_tons": _MATCH_TONS,
        "lat": 40.0,
        "lon": -75.0,
        "target_price": 1000.0,
        "community_id": 7,
        "form": "PALLETS",
        "color": "NATURAL",
        "material_grade": "A",
        "quantity_per_load_lbs": _LOAD_LBS,
    }
    payload = {"query": query, "match_direction": _BUYER, "top_n": 20}
    first = await handle_match(PLASTICOS, payload)
    second = await handle_match(PLASTICOS, payload)

    expected = {
        "facility:fit": _buyer_dims(lat=40.0, credit=100.0, contamination=0.0, community=7, rate=1000.0, dated=True),
        "facility:offset": _buyer_dims(lat=41.0, credit=100.0, contamination=0.0, community=7, rate=1000.0, dated=True),
        "facility:null-capacity": _buyer_dims(
            lat=40.0, credit=100.0, contamination=0.0, community=7, rate=1000.0, dated=True
        ),
        "facility:low-capacity": _buyer_dims(
            lat=40.0, credit=100.0, contamination=0.0, community=7, rate=1000.0, dated=True
        ),
        "facility:null-contamination": _buyer_dims(
            lat=40.0, credit=100.0, contamination=100.0, community=7, rate=1000.0, dated=True
        ),
        "facility:outlier": _buyer_dims(
            lat=40.0, credit=-5.0, contamination=0.0, community=3, rate=1_000_000.0, dated=False
        ),
    }
    assert _weighted(expected["facility:fit"]) == pytest.approx(0.992)  # recency 1.0 under SC-001
    assert _weighted(expected["facility:outlier"]) == pytest.approx(0.52)

    def scored(result: dict) -> dict[str, dict]:
        return {row["candidate"]["facility_id"]: row for row in result["candidates"]}

    got = scored(first)
    assert set(got) == set(expected)
    assert got["facility:fit"]["score"] > got["facility:outlier"]["score"]
    for facility_id, dims in expected.items():
        row = got[facility_id]
        for name, value in dims.items():
            assert row["dimension_scores"][name] == pytest.approx(value, abs=1e-6)
        assert row["score"] == pytest.approx(_weighted(dims), abs=1e-6)
        assert row["score"] == pytest.approx(
            sum(_BUYER_WEIGHTS[name] * row["dimension_scores"][name] for name in _BUYER_WEIGHTS),
            abs=1e-6,
        )
        assert 0.0 <= row["score"] <= 1.0
        assert row["eligible"] is True
        assert row["score_scale"] == "0_to_1"
        assert row["entity_ref"] == facility_id
        flags = row.get("confidence_flag")
        if isinstance(flags, str):
            flags = [flags]
        assert "monoculture" not in list(flags or [])
    assert "facility:pvc-only" not in got
    assert "facility:blank-form" not in got
    assert "facility:no-form" not in got
    assert "facility:intolerant" not in got
    short = got["facility:low-capacity"]["capacity_evidence"]
    assert short["capacity_tons_month"] == pytest.approx(10.0)
    assert short["volume_tons"] == pytest.approx(_MATCH_TONS)
    assert short["shortfall_tons"] == pytest.approx(_MATCH_TONS - 10.0)
    unknown_capacity = got["facility:null-capacity"]["capacity_evidence"]
    assert unknown_capacity["capacity_tons_month"] is None
    assert unknown_capacity["shortfall_tons"] is None
    missing = got["facility:null-contamination"]
    assert "contamination_tolerance_score" in missing["missing_evidence"]
    assert all(item["feature_id"] != "contamination_tolerance_score" for item in missing["feature_contributions"])
    assert first["scoring_meta"]["normalization_applied"] is False
    assert scored(second)["facility:fit"]["score"] == pytest.approx(got["facility:fit"]["score"])
    assert scored(second)["facility:offset"]["dimension_scores"] == pytest.approx(
        got["facility:offset"]["dimension_scores"]
    )

    stored = await graph_driver.execute_query(
        cypher="MATCH (n) WHERE n.quantity_per_load_lbs IS NOT NULL RETURN count(n) AS n",
        parameters={},
        database=PLASTICOS,
    )
    assert stored[0]["n"] == 0


@pytest.mark.asyncio
async def test_declared_recency_feature_on_is_pure_decay(design_pack_deps, graph_driver, monkeypatch):
    """The disabled legacy recency mix is not evidence for the declared-signal path."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "temporalproximity_declared_signals_enabled", True)
    await graph_driver.execute_write(
        cypher=_FACILITY_SEED,
        parameters={
            "batch": [
                {
                    "facility_id": "facility:fit",
                    "stamp_now": True,
                    "props": _props(),
                    "lines": [_line("facility:fit", "in", "PALLETS")],
                }
            ]
        },
        database=PLASTICOS,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        PLASTICOS,
        {
            "query": {
                "polymer_type": "HDPE",
                "contamination_pct": 0.0,
                "volume_tons": _MATCH_TONS,
                "lat": 40.0,
                "lon": -75.0,
                "target_price": 1000.0,
                "community_id": 7,
            },
            "match_direction": _BUYER,
            "top_n": 5,
        },
    )
    row = result["candidates"][0]
    assert row["dimension_scores"]["recency"] == pytest.approx(1.0, abs=1e-6)
    dims = _buyer_dims(lat=40.0, credit=100.0, contamination=0.0, community=7, rate=1000.0, dated=True)
    dims["recency"] = 1.0
    assert row["score"] == pytest.approx(_weighted(dims), abs=1e-6)
    assert row["score"] == pytest.approx(0.992, abs=1e-6)


_OPPORTUNITY_SEED = """
UNWIND $batch AS row
MERGE (s:SupplyOpportunity {opportunity_id: row.opportunity_id})
SET s += row
WITH s
MERGE (p:PolymerFamily {code: s.polymer_type})
MERGE (s)-[:FULFILLS_POLYMER]->(p)
"""


@pytest.mark.asyncio
async def test_supply_match_quantity_is_the_opportunity_payload(design_pack_deps, graph_driver):
    """available_tons is the frozen opportunity quantity. A failing hard gate stays out."""
    await graph_driver.execute_write(
        cypher=_OPPORTUNITY_SEED,
        parameters={
            "batch": [
                {
                    "opportunity_id": "opportunity:approved",
                    "facility_id": "facility:buyer-a",
                    "polymer_type": "HDPE",
                    "available_tons": _OPPORTUNITY_TONS,
                    "lat": 40.0,
                    "lon": -75.0,
                    "rate_per_ton": 500.0,
                    "active": True,
                },
                {
                    "opportunity_id": "opportunity:short",
                    "facility_id": "facility:buyer-a",
                    "polymer_type": "HDPE",
                    "available_tons": 10.0,
                    "lat": 40.0,
                    "lon": -75.0,
                    "rate_per_ton": 500.0,
                    "active": True,
                },
                {
                    "opportunity_id": "opportunity:null-tons",
                    "facility_id": "facility:buyer-a",
                    "polymer_type": "HDPE",
                    "lat": 40.0,
                    "lon": -75.0,
                    "rate_per_ton": 500.0,
                    "active": True,
                },
                {
                    "opportunity_id": "opportunity:inactive",
                    "facility_id": "facility:buyer-a",
                    "polymer_type": "HDPE",
                    "available_tons": _OPPORTUNITY_TONS,
                    "lat": 40.0,
                    "lon": -75.0,
                    "rate_per_ton": 500.0,
                    "active": False,
                },
            ]
        },
        database=PLASTICOS,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        PLASTICOS,
        {
            "query": {
                "polymer_type": "HDPE",
                "volume_tons": _MATCH_TONS,
                "lat": 40.0,
                "lon": -75.0,
                "target_price": 500.0,
                "quantity_per_load_lbs": _LOAD_LBS,
            },
            "match_direction": _SUPPLY,
            "top_n": 10,
        },
    )
    assert [row["candidate"]["opportunity_id"] for row in result["candidates"]] == ["opportunity:approved"]
    row = result["candidates"][0]
    assert row["dimension_scores"]["active_supply_geo"] == pytest.approx(1.0, abs=1e-9)
    assert row["dimension_scores"]["active_supply_price"] == pytest.approx(1.0, abs=1e-9)
    assert row["score"] == pytest.approx(1.0, abs=1e-9)
    assert row["score_scale"] == "0_to_1"
    stored = await graph_driver.execute_query(
        cypher=(
            "MATCH (s:SupplyOpportunity {opportunity_id: 'opportunity:approved'}) "
            "RETURN s.available_tons AS tons, s.quantity_per_load_lbs AS load"
        ),
        parameters={},
        database=PLASTICOS,
    )
    assert stored[0]["tons"] == _OPPORTUNITY_TONS
    assert stored[0]["load"] is None


@pytest.mark.asyncio
async def test_plasticos_source_identity_is_selectable_only_for_a_partner(design_pack_deps, graph_driver):
    """One source partner id, one bare id, and one foreign namespace."""
    from tests.fixtures.match_response_contract import require_unmodified_odoo_mapper

    selectable = "res.partner:102"
    missing = "site-missing"
    foreign = "facility:foreign"
    batch = [
        {
            "facility_id": facility_id,
            "stamp_now": True,
            "props": _props(name=facility_id),
            "lines": [_line(facility_id, "in", "PALLETS")],
        }
        for facility_id in (selectable, missing, foreign)
    ]
    await graph_driver.execute_write(cypher=_FACILITY_SEED, parameters={"batch": batch}, database=PLASTICOS)
    from engine.handlers import handle_match

    result = await handle_match(
        PLASTICOS,
        {
            "query": {
                "polymer_type": "HDPE",
                "density": 0.95,
                "mfi": 8.0,
                "contamination_pct": 0.0,
                "requires_food_grade": False,
                "volume_tons": _MATCH_TONS,
                "lat": 40.0,
                "lon": -75.0,
                "target_price": 1000.0,
                "community_id": 7,
                "form": "PALLETS",
                "color": "NATURAL",
                "material_grade": "A",
                "quantity_per_load_lbs": _LOAD_LBS,
            },
            "match_direction": _BUYER,
            "top_n": 10,
        },
    )
    by_id = {row["candidate"]["facility_id"]: row for row in result["candidates"]}
    assert set(by_id) == {selectable, missing, foreign}
    assert by_id[selectable]["entity_ref"] == selectable
    assert "entity_ref" not in by_id[missing]
    assert by_id[foreign]["entity_ref"] == foreign
    mapped = require_unmodified_odoo_mapper(result)
    assert [item.buyer_partner_id for item in mapped.results] == [102]
    assert {row["entity_ref"] for row in mapped.unresolved} == {None, foreign}
