"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, sync, provenance]
owner: engine-team
status: active
--- /L9_META ---

Value provenance precedence (#351 revision) executed on Neo4j.

Runs ``handle_sync`` on the PlasticOS design pack's facilities endpoint with
``sync_provenance_precedence_enabled`` on, against testcontainers-neo4j. The
Facility property ``tier`` and the PROCESSES line property ``direction`` are
each declared beside an ``enum[observed|enriched]`` companion; every case reads
the stored value and companion back from the graph.
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


def _facility(facility_id: str, **fields: Any) -> dict[str, Any]:
    return {"facility_id": facility_id, "name": facility_id, **fields}


async def _sync(*rows: dict[str, Any]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": list(rows)})
    assert result.get("status") in ("ok", "success"), result


async def _tier(graph_driver: Any, facility_id: str) -> tuple[Any, Any]:
    rows = await graph_driver.execute_query(
        "MATCH (f:Facility {facility_id: $id}) RETURN f.tier AS tier, f.tier_provenance AS provenance",
        parameters={"id": facility_id},
        database=PLASTICOS,
    )
    return rows[0]["tier"], rows[0]["provenance"]


async def _direction(graph_driver: Any, line_id: str) -> tuple[Any, Any]:
    rows = await graph_driver.execute_query(
        "MATCH (:Facility)-[r:PROCESSES {line_id: $line}]->(:PolymerFamily) "
        "RETURN r.direction AS direction, r.direction_provenance AS provenance",
        parameters={"line": line_id},
        database=PLASTICOS,
    )
    assert len(rows) == 1
    return rows[0]["direction"], rows[0]["provenance"]


@pytest.mark.asyncio
async def test_enriched_never_overwrites_observed(design_pack_deps, graph_driver, precedence_on) -> None:
    await _sync(_facility("f-1", tier="premium", tier_provenance="observed"))
    await _sync(_facility("f-1", tier="economy", tier_provenance="enriched"))
    assert await _tier(graph_driver, "f-1") == ("premium", "observed")


@pytest.mark.asyncio
async def test_a_value_with_no_companion_counts_as_enriched(design_pack_deps, graph_driver, precedence_on) -> None:
    await _sync(_facility("f-2", tier="premium", tier_provenance="observed"))
    await _sync(_facility("f-2", tier="economy"))
    assert await _tier(graph_driver, "f-2") == ("premium", "observed")


@pytest.mark.asyncio
async def test_observed_replaces_enriched(design_pack_deps, graph_driver, precedence_on) -> None:
    await _sync(_facility("f-3", tier="economy", tier_provenance="enriched"))
    assert await _tier(graph_driver, "f-3") == ("economy", "enriched")
    await _sync(_facility("f-3", tier="premium", tier_provenance="observed"))
    assert await _tier(graph_driver, "f-3") == ("premium", "observed")


@pytest.mark.asyncio
async def test_observed_replaces_observed_and_enriched_replaces_enriched(
    design_pack_deps, graph_driver, precedence_on
) -> None:
    await _sync(_facility("f-4", tier="premium", tier_provenance="observed"))
    await _sync(_facility("f-4", tier="standard", tier_provenance="observed"))
    assert await _tier(graph_driver, "f-4") == ("standard", "observed")
    await _sync(_facility("f-5", tier="economy", tier_provenance="enriched"))
    await _sync(_facility("f-5", tier="standard", tier_provenance="enriched"))
    assert await _tier(graph_driver, "f-5") == ("standard", "enriched")


@pytest.mark.asyncio
async def test_a_null_never_clears(design_pack_deps, graph_driver, precedence_on) -> None:
    await _sync(_facility("f-6", tier="premium", tier_provenance="enriched"))
    await _sync(_facility("f-6", tier=None, tier_provenance="observed"))
    assert await _tier(graph_driver, "f-6") == ("premium", "enriched")
    await _sync(_facility("f-6"))
    assert await _tier(graph_driver, "f-6") == ("premium", "enriched")


@pytest.mark.asyncio
async def test_line_edge_properties_follow_the_same_precedence(design_pack_deps, graph_driver, precedence_on) -> None:
    def line(direction: Any, provenance: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {"code": "HDPE", "line_id": "f-7-1", "direction": direction, "form": "PELLET"}
        if provenance is not None:
            item["direction_provenance"] = provenance
        return _facility("f-7", processed_polymers=[item])

    await _sync(line("out", "enriched"))
    assert await _direction(graph_driver, "f-7-1") == ("out", "enriched")
    await _sync(line("in", "observed"))
    assert await _direction(graph_driver, "f-7-1") == ("in", "observed")
    await _sync(line("out", "enriched"))
    assert await _direction(graph_driver, "f-7-1") == ("in", "observed")
    await _sync(line(None, "observed"))
    assert await _direction(graph_driver, "f-7-1") == ("in", "observed")
    await _sync(line("out", "observed"))
    assert await _direction(graph_driver, "f-7-1") == ("out", "observed")


@pytest.mark.asyncio
async def test_precedence_off_keeps_last_write_wins(design_pack_deps, graph_driver) -> None:
    assert settings.sync_provenance_precedence_enabled is False
    await _sync(_facility("f-8", tier="premium", tier_provenance="observed"))
    await _sync(_facility("f-8", tier="economy", tier_provenance="enriched"))
    assert await _tier(graph_driver, "f-8") == ("economy", "enriched")
