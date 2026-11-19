"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, match, score, contract]
owner: engine-team
status: active
--- /L9_META ---

G04-SCORE-CONTRACT against Neo4j. ``handle_match`` runs on the synthetic orbital
pack. The returned payload is compared with the draft-unpublished response schema
and decoded by the unmodified Odoo mapper. The match quantity is the frozen
approved supply-opportunity target.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.fixtures.match_response_contract import (
    UNPUBLISHED_REQUIRED,
    frozen_opportunity_quantity,
    schema_messages,
    schema_text,
)
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, depot

pytestmark = pytest.mark.integration

_ODOO = Path(__file__).resolve().parents[3] / "odoo"
if str(_ODOO) not in sys.path:
    sys.path.insert(0, str(_ODOO))

from plasticos_gate.services.gate_mappers import map_match_response  # noqa: E402

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:OrbitalDepot {depot_id: row.depot_id})
SET n += row
SET n.synced_at = datetime()
"""


@pytest.mark.asyncio
async def test_neo4j_match_payload_against_draft_schema_and_odoo_mapper(engine_deps, graph_driver, clean_db):
    quantity = frozen_opportunity_quantity()
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
        {
            "query": {"min_ports": 1, "quantity_lbs": quantity["target"]},
            "match_direction": DIRECTION,
            "top_n": 10,
        },
    )
    assert result["status"] == "success"
    assert result["direction"] == DIRECTION
    assert result["match_direction"] == DIRECTION
    for name in UNPUBLISHED_REQUIRED:
        assert name not in result
    assert "quantity_per_load_lbs" not in result
    messages = schema_messages(result)
    assert "'direction' is a required property" not in messages
    assert "'failed_gates' is a required property" not in messages
    assert schema_text().split("x-publication-status:", 1)[1].splitlines()[0].strip() == "draft-unpublished"

    mapped = map_match_response(result)
    assert mapped.match_direction == DIRECTION
    assert mapped.contract_version is None
    assert [item.entity_ref for item in mapped.results] == ["res.partner:102"]
    selected = mapped.results[0]
    assert selected.buyer_partner_id == 102
    assert selected.eligible is True
    assert selected.gates_failed == []
    assert selected.score_scale == "0_to_1"
    assert selected.normalized_score is not None
    assert 0.0 <= selected.normalized_score <= 1.0
