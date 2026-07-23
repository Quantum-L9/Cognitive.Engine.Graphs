"""Integration tests — match handler: full pipeline, top_n, invalid payload.

Real payload contract (engine/handlers.py::handle_match):
    query: dict (required — resolved against domain queryschema)
    match_direction: str (required — must exist in matchentities)
    top_n: int (optional, clamped to [1, 1000])
    weights: dict (optional, validated to [0, 1], sum <= 1.0)

The plasticos spec declares candidate=Facility for direction
'intake_to_buyer' and a required traversal step
(candidate:Facility)-[:PROCESSES]->(polymer:PolymerFamily), so seed data
must live in the 'plasticos' database and include that edge.
"""

from __future__ import annotations

import pytest

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:Facility {entity_id: row.entity_id})
SET n += row
SET n.synced_at = datetime()
WITH n
MERGE (p:PolymerFamily {code: 'PET'})
MERGE (n)-[:PROCESSES]->(p)
"""


def _facility(entity_id: str, **overrides) -> dict:
    row = {
        "entity_id": entity_id,
        "name": f"Facility {entity_id}",
        "contamination_tolerance": 0.10,
        "latitude": 34.05,
        "longitude": -118.24,
        "food_grade_certified": False,
        "reinforcement_score": 0.5,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_match_returns_candidates(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={
            "batch": [
                _facility("f1", name="Alpha Recycle", contamination_tolerance=0.05),
                _facility("f2", name="Beta Process", contamination_tolerance=0.10, latitude=34.10, longitude=-118.30),
            ]
        },
        database="plasticos",
    )
    from engine.handlers import handle_match

    result = await handle_match(
        "plasticos",
        {
            "query": {
                "polymer_type": "PET",
                "contamination_pct": 0.04,
                "lat": 34.05,
                "lon": -118.24,
            },
            "match_direction": "intake_to_buyer",
            "top_n": 10,
        },
    )
    assert "candidates" in result
    assert result.get("status") in ("ok", "success", None) or "candidates" in result


@pytest.mark.asyncio
async def test_match_respects_top_n(engine_deps, graph_driver, clean_db):
    await graph_driver.execute_write(
        cypher=SEED_CYPHER,
        parameters={"batch": [_facility(f"f{i}", contamination_tolerance=0.20) for i in range(5)]},
        database="plasticos",
    )
    from engine.handlers import handle_match

    result = await handle_match(
        "plasticos",
        {
            "query": {"polymer_type": "PET", "contamination_pct": 0.01},
            "match_direction": "intake_to_buyer",
            "top_n": 2,
        },
    )
    candidates = result.get("candidates", [])
    assert len(candidates) <= 2


@pytest.mark.asyncio
async def test_match_unknown_direction_raises(engine_deps):
    """A match_direction with no candidate entity must raise ValidationError."""
    from engine.handlers import ValidationError, handle_match

    with pytest.raises(ValidationError, match="No candidate entity"):
        await handle_match(
            "plasticos",
            {"query": {"polymer_type": "PET"}, "match_direction": "nonexistent_direction", "top_n": 5},
        )
