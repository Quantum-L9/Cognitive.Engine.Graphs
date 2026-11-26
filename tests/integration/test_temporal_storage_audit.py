"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, health, audit, temporal, read-only]
owner: engine-team
status: active
--- /L9_META ---

The temporal storage detector on real Neo4j (ceg-scoring-audit L1, D1).

State is written by the engine's own ``handle_sync``; raw Cypher only reads
back what was stored (``valueType``), independently of the detector. Holds on
either storage path: before native temporal storage (#563) the synced value is
a STRING and is counted; after it, it is native and is not.
"""

from __future__ import annotations

import pytest

PLASTICOS = "plasticos"
_COUNT_STRINGS = (
    "MATCH (f:Facility) WHERE f.last_transaction_date IS NOT NULL "
    "RETURN count(CASE WHEN valueType(f.last_transaction_date) STARTS WITH 'STRING' THEN 1 END) AS strings, "
    "count(f) AS dated"
)


def _facility(facility_id: str) -> dict:
    return {
        "facility_id": facility_id,
        "name": f"Facility {facility_id}",
        "last_transaction_date": "2026-11-20T12:00:00Z",
        "processed_polymers": [{"code": "PET", "line_id": f"{facility_id}-pet"}],
    }


@pytest.mark.integration
async def test_the_detector_counts_exactly_the_stored_strings_and_writes_nothing(
    design_pack_deps, graph_driver, plasticos_domain_spec
):
    from engine.handlers import handle_sync
    from engine.health.temporal_storage_audit import audit_temporal_storage

    batch = [_facility("a"), _facility("b")]
    assert (await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": batch}))["status"] == "success"
    stored = (await graph_driver.execute_query(_COUNT_STRINGS, database=PLASTICOS))[0]
    assert stored["dated"] == 2
    before = await graph_driver.execute_query(
        "MATCH (n) WITH n ORDER BY elementId(n) RETURN count(n) AS nodes, collect(n.last_transaction_date) AS values",
        database=PLASTICOS,
    )

    report = await audit_temporal_storage(graph_driver, plasticos_domain_spec)

    finding = next(f for f in report["findings"] if f["property"] == "last_transaction_date")
    assert finding["string_valued"] == stored["strings"]
    assert report["conversion_required"] is (report["string_valued_total"] > 0)
    after = await graph_driver.execute_query(
        "MATCH (n) WITH n ORDER BY elementId(n) RETURN count(n) AS nodes, collect(n.last_transaction_date) AS values",
        database=PLASTICOS,
    )
    assert after == before  # read-only: nothing created, removed or converted
