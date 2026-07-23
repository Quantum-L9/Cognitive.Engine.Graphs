"""Integration tests — outcomes handler: success, failure, validation.

Real payload contract (engine/handlers.py::handle_outcomes):
    match_id: str (required)
    candidate_id: str (required)
    outcome: "success" | "failure" | "partial" (required)
    value: float | None (optional)
    fingerprint: dict (optional R5 match fingerprint —
        active_dimensions, dimension_weights, gates_passed,
        match_direction, candidate_count)
Returns {"status": "recorded", "outcome_id": "out_..."}.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_outcome_success_recorded(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher="MERGE (a:Facility {entity_id: $aid}) MERGE (b:Facility {entity_id: $bid})",
        parameters={"aid": "match-001", "bid": "cand-001"},
        database="plasticos",
    )
    from engine.handlers import handle_outcomes

    result = await handle_outcomes(
        "plasticos",
        {
            "match_id": "match-001",
            "candidate_id": "cand-001",
            "outcome": "success",
            "value": 0.85,
            "fingerprint": {
                "active_dimensions": ["geo_proximity"],
                "dimension_weights": {"geo_proximity": 0.85},
                "gates_passed": ["contamination_threshold"],
                "match_direction": "intake_to_buyer",
                "candidate_count": 2,
            },
        },
    )
    assert result.get("status") == "recorded"
    assert result.get("outcome_id", "").startswith("out_")


@pytest.mark.asyncio
async def test_outcome_failure_recorded(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher="MERGE (a:Facility {entity_id: $aid}) MERGE (b:Facility {entity_id: $bid})",
        parameters={"aid": "match-002", "bid": "cand-002"},
        database="plasticos",
    )
    from engine.handlers import handle_outcomes

    result = await handle_outcomes(
        "plasticos",
        {
            "match_id": "match-002",
            "candidate_id": "cand-002",
            "outcome": "failure",
        },
    )
    assert result.get("status") == "recorded"
    assert result.get("outcome_id", "").startswith("out_")


@pytest.mark.asyncio
async def test_outcome_invalid_value_raises(engine_deps):
    """Invalid outcome literal is rejected by handler validation."""
    from engine.handlers import ValidationError, handle_outcomes

    with pytest.raises(ValidationError, match="Invalid outcome"):
        await handle_outcomes(
            "plasticos",
            {
                "match_id": "match-003",
                "candidate_id": "cand-003",
                "outcome": "yes_please",
            },
        )
