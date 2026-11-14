"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, causal, attribution]
owner: engine-team
status: active
--- /L9_META ---

Integration test — SI-27 time-ordered causal attribution on live Neo4j.

Seeds one outcome reached by three touchpoints: ``near`` (1 hop, occurred
2026-05-01), ``old`` (2 hops, occurred 2026-01-01) and ``undated`` (1 hop, no
timestamp), on the synthetic orbital-logistics pack with a RESULTED_IN /
CHAINED_TO causal declaration whose source label declares
``source_timestamp_property: occurred_at``. With
``causal_attribution_time_ordered`` off, first_touch credits the nearest
touchpoint; on, it credits the earliest one and the undated touchpoint is
excluded and counted.

Requires a live Neo4j; skips when testcontainers / NEO4J_TEST_URI is unavailable.
"""

from __future__ import annotations

import pytest

from engine.causal.attribution import AttributionCalculator
from engine.config.schema import CausalEdgeSpec
from engine.config.settings import settings

pytestmark = pytest.mark.integration

SEED_CYPHER = """
MERGE (o:DockingResult {outcome_id: $outcome_id, entity_id: $outcome_id})
MERGE (near:OrbitalDepot {entity_id: 'near'}) SET near.occurred_at = datetime('2026-05-01T00:00:00Z')
MERGE (old:OrbitalDepot {entity_id: 'old'}) SET old.occurred_at = datetime('2026-01-01T00:00:00Z')
MERGE (undated:OrbitalDepot {entity_id: 'undated'})
MERGE (near)-[:RESULTED_IN {confidence: 1.0}]->(o)
MERGE (undated)-[:RESULTED_IN {confidence: 1.0}]->(o)
MERGE (old)-[:CHAINED_TO {confidence: 1.0}]->(near)
"""


def _spec(domain_loader):
    spec = domain_loader.load_domain("orbital-logistics")
    edges = [
        CausalEdgeSpec(
            edge_type="RESULTED_IN",
            source_label="OrbitalDepot",
            target_label="DockingResult",
            source_timestamp_property="occurred_at",
        ),
        CausalEdgeSpec(
            edge_type="CHAINED_TO",
            source_label="OrbitalDepot",
            target_label="OrbitalDepot",
            source_timestamp_property="occurred_at",
        ),
    ]
    causal = spec.causal.model_copy(update={"causal_edges": edges})
    return spec.model_copy(update={"causal": causal})


@pytest.mark.asyncio
async def test_first_touch_orders_by_event_time(
    engine_deps, graph_driver, domain_loader, clean_db, monkeypatch
) -> None:
    spec = _spec(domain_loader)
    outcome_id = "ot-si27"
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"outcome_id": outcome_id},
        database=spec.domain.id,
    )
    calc = AttributionCalculator(graph_driver, spec)

    monkeypatch.setattr(settings, "causal_attribution_time_ordered", False)
    legacy = await calc.compute_attribution(outcome_id, model="first_touch")
    assert legacy["touchpoints"]["old"] == 0.0
    assert "untimestamped_touchpoints" not in legacy

    monkeypatch.setattr(settings, "causal_attribution_time_ordered", True)
    fixed = await calc.compute_attribution(outcome_id, model="first_touch")
    assert fixed["touchpoints"] == {"old": 1.0, "near": 0.0}
    assert fixed["untimestamped_touchpoints"] == 1
    assert fixed["ordering"] == "event_timestamp"

    last = await calc.compute_attribution(outcome_id, model="last_touch")
    assert last["touchpoints"] == {"old": 0.0, "near": 1.0}
