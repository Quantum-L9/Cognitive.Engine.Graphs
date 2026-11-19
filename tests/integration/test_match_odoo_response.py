"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, match, odoo, response]
owner: engine-team
status: active
--- /L9_META ---

G01-CEG-ODOO-RESPONSE against Neo4j. ``handle_match`` runs on the synthetic
orbital pack; its payload is decoded by the unmodified Odoo mapper.
"""

from __future__ import annotations

import pytest

from tests.fixtures.match_response_contract import map_match_if_sibling, partner_id
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, depot

pytestmark = pytest.mark.integration

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:OrbitalDepot {depot_id: row.depot_id})
SET n += row
SET n.synced_at = datetime()
"""


@pytest.mark.asyncio
async def test_neo4j_match_is_selectable_by_the_unmodified_odoo_mapper(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={
            "batch": [
                depot("buyer", reliability=0.9, entity_ref="res.partner:102"),
                depot("bare", reliability=0.2),
            ]
        },
        database=ORBITAL,
    )
    from engine.handlers import handle_match

    result = await handle_match(
        ORBITAL,
        {"query": {"min_ports": 1}, "match_direction": DIRECTION, "top_n": 10},
    )
    assert "contract_version" not in result
    assert "quantity_per_load_lbs" not in result

    selected_row = next(row for row in result["candidates"] if row.get("entity_ref") == "res.partner:102")
    assert partner_id(selected_row["entity_ref"]) == 102
    bare = next(row for row in result["candidates"] if row["candidate"]["depot_id"] == "bare")
    assert "entity_ref" not in bare

    mapped = map_match_if_sibling(result)
    if mapped is None:
        return
    assert [item.entity_ref for item in mapped.results] == ["res.partner:102"]
    selected = mapped.results[0]
    assert selected.buyer_partner_id == 102
    assert selected.eligible is True
    assert selected.rank == 1
    assert selected.score_scale == "0_to_1"
    assert selected.normalized_score is not None
    assert 0.0 <= selected.normalized_score <= 1.0
    contributed = {item["feature_id"] for item in selected.feature_contributions}
    assert contributed.isdisjoint(selected.missing_evidence)
    assert any("no entity_ref" in row["reason"] for row in mapped.unresolved)
