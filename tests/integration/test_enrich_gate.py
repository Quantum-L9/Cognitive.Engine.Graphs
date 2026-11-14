"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, enrich, sync, integration]
owner: engine-team
status: active
--- /L9_META ---

CEG-ENRICH-GATE against Neo4j: a sync-written property survives every enrich
attempt — with the local writer off, and with it on — while a property no sync
endpoint owns is still writable when the operator turns the writer on.
"""

from __future__ import annotations

import pytest

ORBITAL = "orbital-logistics"


async def _depot(graph_driver) -> dict:
    rows = await graph_driver.execute_query(
        "MATCH (d:OrbitalDepot {depot_id: '7001'}) "
        "RETURN d.reliability AS reliability, d.docking_ports AS ports, d.enrich_note AS note",
        database=ORBITAL,
    )
    assert len(rows) == 1
    return dict(rows[0])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sync_written_property_survives_enrich(engine_deps, graph_driver, clean_db, monkeypatch):
    from engine.config.settings import settings
    from engine.handlers import FeatureNotEnabled, ValidationError, handle_enrich, handle_sync

    synced = await handle_sync(
        ORBITAL,
        {"entity_type": "orbital-depots", "batch": [{"depot_id": "7001", "docking_ports": 4, "reliability": 0.91}]},
    )
    assert synced.get("status") in ("ok", "success")
    before = await _depot(graph_driver)
    assert before == {"reliability": 0.91, "ports": 4, "note": None}

    overwrite = {
        "entity_type": "OrbitalDepot",
        "enrichments": [{"property": "reliability", "expression": "0.0"}],
    }

    # Writer off (the default): typed refusal, nothing written.
    monkeypatch.setattr(settings, "enrich_local_writer_enabled", False)
    with pytest.raises(FeatureNotEnabled, match="ENRICH_LOCAL_WRITER_ENABLED"):
        await handle_enrich(ORBITAL, overwrite)
    assert await _depot(graph_driver) == before

    # Writer on: the sync-owned property is refused before any Cypher runs.
    monkeypatch.setattr(settings, "enrich_local_writer_enabled", True)
    with pytest.raises(ValidationError, match="sync endpoint of the pack owns"):
        await handle_enrich(ORBITAL, overwrite)
    mixed = {
        "entity_type": "OrbitalDepot",
        "enrichments": [
            {"property": "enrich_note", "expression": "n.docking_ports * 2"},
            {"property": "reliability", "expression": "0.0"},
        ],
    }
    with pytest.raises(ValidationError, match="sync endpoint of the pack owns"):
        await handle_enrich(ORBITAL, mixed)
    assert await _depot(graph_driver) == before

    # Writer on, unowned property: written; the sync-written values are intact.
    result = await handle_enrich(
        ORBITAL,
        {
            "entity_type": "OrbitalDepot",
            "enrichments": [{"property": "enrich_note", "expression": "n.docking_ports * 2"}],
        },
    )
    assert result["enriched_count"] == 1
    assert await _depot(graph_driver) == {"reliability": 0.91, "ports": 4, "note": 8}
