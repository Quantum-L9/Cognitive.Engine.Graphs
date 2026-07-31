"""Integration test — causal attribution temporal decay (Contract 17 / C-21).

Exercises the `causal.temporal_decay_enabled` consumer against a live Neo4j
(testcontainers). Seeds one outcome and two touchpoints whose causal links have
different ``created_at`` timestamps, then verifies:

  * with the flag ON, the touchpoint reached through the RECENT causal link gets
    strictly more attribution than the one reached through the OLD link, and the
    weights still sum to 1.0 (renormalized);
  * with the flag OFF (default), the linear model gives them equal credit.

Requires a live Neo4j; skips automatically when testcontainers / NEO4J_TEST_URI
is unavailable (see tests/conftest.py::neo4j_container).
"""

from __future__ import annotations

import pytest

from engine.causal.attribution import AttributionCalculator

# touchpoint -> outcome causal links; tp_recent is fresh, tp_old is one year old.
SEED_CYPHER = """
MERGE (o:TransactionOutcome {outcome_id: $outcome_id, entity_id: $outcome_id})
MERGE (r:Facility {entity_id: $recent_id})
MERGE (s:Facility {entity_id: $old_id})
MERGE (r)-[er:RESULTED_IN]->(o)
SET er.confidence = 1.0, er.created_at = datetime()
MERGE (s)-[es:RESULTED_IN]->(o)
SET es.confidence = 1.0, es.created_at = datetime() - duration({days: 365})
"""


def _decay_enabled(spec):
    """Return a copy of `spec` with causal.temporal_decay_enabled flipped on
    (CausalSubgraphSpec is frozen, so copy-with-update)."""
    causal_on = spec.causal.model_copy(update={"temporal_decay_enabled": True})
    return spec.model_copy(update={"causal": causal_on})


@pytest.mark.asyncio
async def test_temporal_decay_favors_recent_touchpoint(engine_deps, graph_driver, domain_loader, clean_db):
    spec = domain_loader.load_domain("plasticos")
    outcome_id = "ot-decay-recent"
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"outcome_id": outcome_id, "recent_id": "tp_recent", "old_id": "tp_old"},
        database=spec.domain.id,
    )

    # Flag ON: recent link out-weighs the old link, weights renormalized to 1.0.
    calc_on = AttributionCalculator(graph_driver, _decay_enabled(spec))
    res_on = await calc_on.compute_attribution(outcome_id, model="linear")
    tps_on = res_on["touchpoints"]
    assert tps_on["tp_recent"] > tps_on["tp_old"]
    assert sum(tps_on.values()) == pytest.approx(1.0, abs=1e-6)
    assert res_on.get("temporal_decay", {}).get("enabled") is True

    # Flag OFF (default): linear model gives equal credit.
    calc_off = AttributionCalculator(graph_driver, spec)
    res_off = await calc_off.compute_attribution(outcome_id, model="linear")
    tps_off = res_off["touchpoints"]
    assert tps_off["tp_recent"] == pytest.approx(tps_off["tp_old"], abs=1e-6)
    assert "temporal_decay" not in res_off
