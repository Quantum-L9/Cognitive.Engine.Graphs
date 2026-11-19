"""Integration tests — sync handler: merge, idempotency, unknown entity.

Core behaviour runs on the synthetic orbital-logistics pack (ADR-112 Stage 4,
A16). The last test guards the plasticos design pack's own facilities endpoint
declaration, so it loads that pack explicitly.
"""

from __future__ import annotations

import pytest

ORBITAL = "orbital-logistics"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sync_merges_depots(engine_deps, graph_driver, clean_db):
    from engine.handlers import handle_sync

    result = await handle_sync(
        ORBITAL,
        {
            "entity_type": "orbital-depots",
            # Spec sync endpoint /v1/sync/orbital-depots declares idproperty: depot_id
            "batch": [
                {"depot_id": "9001", "docking_ports": 3, "reliability": 0.93},
                {"depot_id": "9002", "docking_ports": 5, "reliability": 0.97},
            ],
        },
    )
    assert result.get("status") in ("ok", "success")
    rows = await graph_driver.execute_query(
        "MATCH (d:OrbitalDepot) RETURN d.depot_id AS id ORDER BY id", database=ORBITAL
    )
    assert [r["id"] for r in rows] == ["9001", "9002"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sync_idempotent_on_second_call(engine_deps, graph_driver, clean_db):
    from engine.handlers import handle_sync

    payload = {
        "entity_type": "orbital-depots",
        "batch": [{"depot_id": "9003", "docking_ports": 2}],
    }
    r1 = await handle_sync(ORBITAL, payload)
    r2 = await handle_sync(ORBITAL, payload)
    assert r1.get("status") in ("ok", "success")
    assert r2.get("status") in ("ok", "success")
    rows = await graph_driver.execute_query(
        "MATCH (d:OrbitalDepot {depot_id: '9003'}) RETURN count(d) AS n", database=ORBITAL
    )
    assert rows[0]["n"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_unknown_entity_type_raises(domain_loader):
    """RULE 3: an unknown entity_type is rejected, not silently passed through.

    handle_sync resolves the entity_type to a declared sync endpoint and raises
    ValidationError when none matches. This rejection happens during endpoint
    resolution — before any Neo4j access — so the real domain loader plus a mock
    driver is sufficient (no testcontainers needed).
    """
    from unittest.mock import AsyncMock

    from engine.handlers import ValidationError, handle_sync, init_dependencies
    from engine.state import get_state

    get_state().reset()
    init_dependencies(AsyncMock(), domain_loader)
    try:
        with pytest.raises(ValidationError, match="No sync endpoint for entity type"):
            await handle_sync(
                ORBITAL,
                {
                    "entity_type": "nonexistent_entity_type_xyz",
                    "batch": [{"depot_id": "9001"}],
                },
            )
    finally:
        get_state().reset()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_synced_facility_matches_on_its_processed_polymer(design_pack_deps):
    """RG-062 (RU-048 / CEG-COR-071): sync then match returns the synced facility.

    Pack-specific: guards the plasticos facilities endpoint's PROCESSES childsync
    declaration, so it runs on that design pack (``design_pack_deps``).

    The facilities endpoint links each listed polymer with a PROCESSES edge,
    which the plasticos polymer gate requires; before, synced facilities had no
    polymer edges and no engine-synced facility could ever match.
    """
    from engine.handlers import handle_match, handle_sync

    synced = await handle_sync(
        "plasticos",
        {
            "entity_type": "facilities",
            "batch": [
                {
                    "facility_id": "sync-pet",
                    "name": "Synced PET Facility",
                    "contamination_tolerance": 0.10,
                    "direction": "in",
                    "form": "PELLET",
                    "processed_polymers": [{"code": "PET", "line_id": "pet-1"}],
                },
                {
                    "facility_id": "sync-pp",
                    "name": "Synced PP Facility",
                    "contamination_tolerance": 0.10,
                    "direction": "in",
                    "form": "PELLET",
                    "processed_polymers": [{"code": "PP", "line_id": "pp-1"}],
                },
                {
                    "facility_id": "site-in",
                    "name": "HDPE buyer",
                    "entity_ref": "res.partner:102",
                    "direction": "in",
                    "form": "PELLET",
                    "processed_polymers": [{"code": "HDPE", "line_id": "hdpe-1"}],
                },
                {
                    "facility_id": "site-short",
                    "name": "Short HDPE buyer",
                    "entity_ref": "res.partner:103",
                    "capacity_tons_month": 5.0,
                    "contamination_tolerance": 0.10,
                    "direction": "in",
                    "form": "PELLET",
                    "processed_polymers": [{"code": "HDPE", "line_id": "hdpe-short"}],
                },
                {
                    "facility_id": "site-pvc",
                    "name": "PVC only",
                    "entity_ref": "res.partner:104",
                    "capacity_tons_month": 100.0,
                    "contamination_tolerance": 0.10,
                    "direction": "in",
                    "form": "PELLET",
                    "processed_polymers": [{"code": "PVC", "line_id": "pvc-1"}],
                },
                {
                    "facility_id": "site-dirty",
                    "name": "Known incompatible HDPE buyer",
                    "contamination_tolerance": 0.01,
                    "direction": "in",
                    "form": "PELLET",
                    "processed_polymers": [{"code": "HDPE", "line_id": "hdpe-dirty"}],
                },
            ],
        },
    )
    assert synced.get("status") in ("ok", "success")

    result = await handle_match(
        "plasticos",
        {
            "query": {"polymer_type": "PET", "contamination_pct": 0.04},
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 10,
        },
    )
    ids = [c["candidate"]["facility_id"] for c in result.get("candidates", [])]
    assert ids == ["sync-pet"]

    hdpe = await handle_match(
        "plasticos",
        {
            "query": {
                "polymer_type": "HDPE",
                "contamination_pct": 0.04,
                "volume_tons": 18.0,
                "color": "NATURAL",
                "material_grade": "A",
                "form": "REGRIND",
            },
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 10,
        },
    )
    by_id = {row["candidate"]["facility_id"]: row for row in hdpe.get("candidates", [])}
    assert set(by_id) == {"site-in", "site-short"}
    assert by_id["site-in"]["entity_ref"] == "res.partner:102"
    assert "contamination_tolerance_score" in by_id["site-in"]["missing_evidence"]
    assert by_id["site-in"]["capacity_evidence"]["capacity_tons_month"] is None
    assert by_id["site-in"]["capacity_evidence"]["shortfall_tons"] is None
    assert by_id["site-short"]["entity_ref"] == "res.partner:103"
    assert by_id["site-short"]["capacity_evidence"]["shortfall_tons"] == 13.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_changing_a_capability_line_polymer_drops_the_old_match(design_pack_deps):
    """A line that keeps its line_id and changes polymer must not keep the old match."""
    from engine.handlers import handle_match, handle_sync

    facility_id = "rebind-line"
    line_id = "line-rebind"
    payload = {
        "entity_type": "facilities",
        "batch": [
            {
                "facility_id": facility_id,
                "name": "Rebinding buyer",
                "contamination_tolerance": 0.10,
                "direction": "in",
                "form": "PELLET",
                "processed_polymers": [{"code": "HDPE", "line_id": line_id, "direction": "in", "form": "PELLET"}],
            }
        ],
    }
    first = await handle_sync("plasticos", payload)
    assert first.get("status") in ("ok", "success")

    async def match_ids(polymer: str) -> list[str]:
        result = await handle_match(
            "plasticos",
            {
                "query": {"polymer_type": polymer, "contamination_pct": 0.04},
                "match_direction": "supply_opportunity_to_buyer_facility",
                "top_n": 10,
            },
        )
        return [row["candidate"]["facility_id"] for row in result.get("candidates", [])]

    assert facility_id in await match_ids("HDPE")

    payload["batch"][0]["processed_polymers"] = [
        {"code": "PP", "line_id": line_id, "direction": "in", "form": "PELLET"}
    ]
    second = await handle_sync("plasticos", payload)
    assert second.get("status") in ("ok", "success")
    assert facility_id not in await match_ids("HDPE")
    assert facility_id in await match_ids("PP")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keyed_lines_create_update_replace_and_incoming_reassignment(design_pack_deps, graph_driver):
    """Present lists replace keyed lines. Omission does not. Incoming reassignment drops the old target."""
    from engine.config.loader import DomainPackLoader
    from engine.handlers import handle_sync
    from engine.sync.generator import SyncGenerator
    from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

    def line(code: str, form: str, line_id: str = "keep") -> dict[str, str]:
        return {"code": code, "line_id": line_id, "direction": "in", "form": form}

    async def sync(polymers: list[dict[str, str]] | None) -> None:
        row: dict[str, object] = {"facility_id": "line-set", "name": "Line set", "contamination_tolerance": 0.10}
        if polymers is not None:
            row["processed_polymers"] = polymers
        status = (await handle_sync("plasticos", {"entity_type": "facilities", "batch": [row]})).get("status")
        assert status in ("ok", "success")

    async def outgoing() -> list[tuple[object, object, object]]:
        found = await graph_driver.execute_query(
            "MATCH (n:Facility {facility_id: 'line-set'})-[r:PROCESSES]->(c) "
            "RETURN r.line_id AS line_id, c.code AS code, r.form AS form ORDER BY line_id",
            {},
            "plasticos",
        )
        return [(row["line_id"], row["code"], row["form"]) for row in found]

    await sync([line("HDPE", "PELLET")])
    await sync([line("HDPE", "BALE"), line("PP", "PELLET", "drop")])
    await sync([line("HDPE", "BALE")])
    assert await outgoing() == [("keep", "HDPE", "BALE")]
    await sync(None)
    assert await outgoing() == [("keep", "HDPE", "BALE")]
    await sync([])
    assert await outgoing() == []
    spec = DomainPackLoader(str(DESIGN_PACKS_ROOT)).load_domain("plasticos")
    endpoint = next(item for item in spec.sync.endpoints if item.path == "/v1/sync/facilities")
    child = endpoint.childsync[0].model_copy(update={"edgedirection": "childtoparent"})
    generator = SyncGenerator(spec)
    clauses = generator._declared_child_clauses(endpoint.path, child, generator.row_contract(endpoint).children[0])
    cypher = (
        "UNWIND $batch AS row MERGE (n:Facility {facility_id: row.node.facility_id}) SET n += row.node, n._tenant = $tenant "
        + " ".join(clauses)
    )
    for code in ("HDPE", "PP"):
        projected, _dropped = generator.project_batch(
            endpoint,
            [
                {
                    "facility_id": "incoming-buyer",
                    "name": "Incoming",
                    "processed_polymers": [line(code, "PELLET", "in-line")],
                }
            ],
        )
        await graph_driver.execute_query(
            cypher=cypher, parameters={"batch": projected, "tenant": "plasticos"}, database="plasticos"
        )
    incoming = await graph_driver.execute_query(
        "MATCH (n:Facility {facility_id: 'incoming-buyer'})<-[r:PROCESSES]-(c) RETURN c.code AS code", {}, "plasticos"
    )
    assert [row["code"] for row in incoming] == ["PP"]
