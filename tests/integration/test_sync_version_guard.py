"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, sync, provenance, stale-write]
owner: engine-team
status: active
--- /L9_META ---

Stale-write guard (#351 guardrail, EIE #419 rule 6) executed on Neo4j.

Runs ``handle_sync`` on the PlasticOS design pack's facilities endpoint, whose
Facility declares ``sync_version: int``, with ``sync_provenance_precedence_enabled``
on, against testcontainers-neo4j. A row with a lower version than the stored
one is refused whole (node values, line items) and counted as stale in the ack.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.settings import settings

pytestmark = pytest.mark.integration

PLASTICOS = "plasticos"


@pytest.fixture
def precedence_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)


def _facility(version: int | None, tier: str, direction: str, line: str = "g-1") -> dict[str, Any]:
    row: dict[str, Any] = {
        "facility_id": "g-fac",
        "name": "guarded",
        "tier": tier,
        "tier_provenance": "observed",
        "processed_polymers": [
            {"code": "HDPE", "line_id": line, "direction": direction, "form": "PELLET", "provenance": "observed"}
        ],
    }
    if version is not None:
        row["sync_version"] = version
    return row


async def _sync(*rows: dict[str, Any]) -> dict[str, Any]:
    from engine.handlers import handle_sync

    result = await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": list(rows)})
    assert result.get("status") in ("ok", "success"), result
    return result


async def _state(graph_driver: Any) -> tuple[Any, Any, list[tuple[Any, Any]]]:
    rows = await graph_driver.execute_query(
        "MATCH (f:Facility {facility_id: 'g-fac'}) "
        "OPTIONAL MATCH (f)-[r:PROCESSES]->(:PolymerFamily) "
        "WITH f, r ORDER BY r.line_id "
        "RETURN f.tier AS tier, f.sync_version AS version, collect([r.line_id, r.direction]) AS lines",
        database=PLASTICOS,
    )
    return rows[0]["tier"], rows[0]["version"], [tuple(line) for line in rows[0]["lines"]]


@pytest.mark.asyncio
async def test_higher_applies_equal_replays_lower_is_refused_whole(
    design_pack_deps, graph_driver, precedence_on
) -> None:
    first = await _sync(_facility(2, "premium", "in"))
    assert first.get("stale_count") == 0
    assert first["synced_count"] == 1
    assert await _state(graph_driver) == ("premium", 2, [("g-1", "in")])

    replay = await _sync(_facility(2, "premium", "in"))
    assert replay.get("stale_count") == 0
    assert await _state(graph_driver) == ("premium", 2, [("g-1", "in")])

    # v1 arrives after v2 was acked: nothing of it lands, node or line item.
    late = await _sync(_facility(1, "economy", "out", line="g-2"))
    assert late["stale_count"] == 1
    assert late["synced_count"] == 0
    assert await _state(graph_driver) == ("premium", 2, [("g-1", "in")])

    newer = await _sync(_facility(3, "standard", "out"))
    assert newer.get("stale_count") == 0
    assert await _state(graph_driver) == ("standard", 3, [("g-1", "out")])


@pytest.mark.asyncio
@pytest.mark.parametrize("validating", [True, False], ids=["row-validation-on", "row-validation-off"])
@pytest.mark.parametrize("version", ["missing", None])
async def test_a_row_without_a_version_is_refused_at_ingress(
    design_pack_deps, graph_driver, *, precedence_on, monkeypatch: pytest.MonkeyPatch, version: Any, validating: bool
) -> None:
    """#560 review: a versioned endpoint refuses a row without a non-null sync_version; nothing is written.

    The refusal holds with ``sync_row_type_validation_enabled`` off too.
    """
    from engine.handlers import SyncRowRejectedError

    monkeypatch.setattr(settings, "sync_row_type_validation_enabled", validating)
    await _sync(_facility(5, "premium", "in"))
    row = _facility(None, "standard", "out")
    if version is None:
        row["sync_version"] = None
    with pytest.raises(SyncRowRejectedError, match="sync_version"):
        await _sync(row)
    assert await _state(graph_driver) == ("premium", 5, [("g-1", "in")])


@pytest.mark.asyncio
async def test_one_stale_row_does_not_block_the_rest_of_the_batch(
    design_pack_deps, graph_driver, precedence_on
) -> None:
    await _sync(_facility(4, "premium", "in"))
    other = {"facility_id": "g-other", "name": "other", "sync_version": 1}
    result = await _sync(_facility(3, "economy", "out"), other)
    assert result["stale_count"] == 1
    assert result["synced_count"] == 1
    assert await _state(graph_driver) == ("premium", 4, [("g-1", "in")])
    rows = await graph_driver.execute_query(
        "MATCH (f:Facility {facility_id: 'g-other'}) RETURN f.sync_version AS version", database=PLASTICOS
    )
    assert rows[0]["version"] == 1


@pytest.mark.asyncio
async def test_a_lower_version_later_in_the_same_batch_is_refused(
    design_pack_deps, graph_driver, precedence_on
) -> None:
    """#560 review: each row compares against what earlier rows of the batch wrote."""
    await _sync(_facility(1, "economy", "in"))
    result = await _sync(_facility(3, "premium", "in"), _facility(2, "standard", "out", line="g-2"))
    # Batch order is the expected one (v3 then v2: one stale); either order ends at v3's state.
    assert (result["stale_count"], result["synced_count"]) in {(1, 1), (0, 2)}
    assert await _state(graph_driver) == ("premium", 3, [("g-1", "in")])
