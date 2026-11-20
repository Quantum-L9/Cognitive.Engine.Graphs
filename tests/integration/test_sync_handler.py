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
