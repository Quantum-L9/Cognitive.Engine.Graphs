"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [ingestion, sync]
status: active
--- /L9_META ---

Integration tests — sync handler: merge, idempotency, unknown entity.
"""

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


def test_sync_unknown_entity_type_raises(domain_loader):
    """RULE 3: unknown entity_type raises, not silent pass-through."""
    from engine.sync.generator import SyncGenerator

    spec = domain_loader.load_domain("plasticos")
    gen = SyncGenerator(spec)
    with pytest.raises(Exception):
        gen.resolve_endpoint("nonexistent_entity_type_xyz")
