"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, handlers, match, gates, traversal]
owner: engine-team
status: active
--- /L9_META ---

RU-011 / CEG-COR-033 (RG-014): a gate-failing candidate is excluded even when
the final traversal step is ``required: false``.

Uses a copy of the plasticos pack with a trailing optional PROCESSES step. The
served pack constrains polymers with an EXISTS gate since RU-040 (CEG-COR-013),
so the step is added back here and both facilities process the requested
polymer. Without the ``WITH *`` row boundary the contamination gate binds to the
OPTIONAL MATCH and the failing facility is returned with a null polymer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.config.schema import TraversalStepSpec

pytestmark = pytest.mark.integration

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:Facility {facility_id: row.facility_id})
SET n += row
WITH n
MERGE (p:PolymerFamily {code: 'PET'})
MERGE (n)-[:PROCESSES]->(p)
"""

DIRECTION = "supply_opportunity_to_buyer_facility"


def _optional_last_step(spec):
    from engine.config.schema import TraversalSpec

    step = TraversalStepSpec(
        name="polymer_capability",
        pattern="(candidate:Facility)-[:PROCESSES]->(polymer:PolymerFamily)",
        required=False,
        matchdirections=[DIRECTION],
    )
    return spec.model_copy(update={"traversal": TraversalSpec(steps=[step])})


@pytest.mark.asyncio
async def test_gate_failing_candidate_excluded_with_optional_last_step(
    engine_deps, graph_driver, domain_loader, clean_db
):
    from engine.handlers import handle_match, init_dependencies

    spec = _optional_last_step(domain_loader.load_domain("plasticos"))
    loader = MagicMock()
    loader.load_domain.return_value = spec
    init_dependencies(graph_driver, loader)

    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={
            "batch": [
                {"facility_id": "f_pass", "name": "Pass", "contamination_tolerance": 0.10},
                {"facility_id": "f_fail", "name": "Fail", "contamination_tolerance": 0.01},
            ]
        },
        database="plasticos",
    )

    result = await handle_match(
        "plasticos",
        {
            "query": {"polymer_type": "PET", "contamination_pct": 0.05},
            "match_direction": DIRECTION,
            "top_n": 10,
        },
    )
    ids = {c["candidate"].get("facility_id") for c in result["candidates"]}
    assert "f_fail" not in ids
    assert "f_pass" in ids
