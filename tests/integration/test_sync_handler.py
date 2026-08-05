"""Integration tests — sync handler: merge, idempotency, unknown entity."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_sync_merges_facilities(engine_deps, clean_db):
    from engine.handlers import handle_sync

    result = await handle_sync(
        "plasticos",
        {
            "entity_type": "facilities",
            # Spec sync endpoint /v1/sync/facilities declares idproperty: facility_id
            "batch": [
                {"facility_id": 9001, "name": "Omega Plastics", "contamination_tolerance": 0.03},
                {"facility_id": 9002, "name": "Delta Recycle", "contamination_tolerance": 0.07},
            ],
        },
    )
    assert result.get("status") in ("ok", "success")


@pytest.mark.asyncio
async def test_sync_idempotent_on_second_call(engine_deps, clean_db):
    from engine.handlers import handle_sync

    payload = {
        "entity_type": "facilities",
        "batch": [{"facility_id": 9003, "name": "Idem Facility"}],
    }
    r1 = await handle_sync("plasticos", payload)
    r2 = await handle_sync("plasticos", payload)
    assert r1.get("status") in ("ok", "success")
    assert r2.get("status") in ("ok", "success")


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
                "plasticos",
                {
                    "entity_type": "nonexistent_entity_type_xyz",
                    "batch": [{"facility_id": 9001}],
                },
            )
    finally:
        get_state().reset()
