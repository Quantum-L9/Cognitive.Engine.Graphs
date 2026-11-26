"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, outcomes, scoring, feedback]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_outcome_preference_history.py
L8 item 3, producer side, on a real graph: an outcome recorded by
``handle_outcomes`` with ``query_entity_id`` is stored in the shape
``preference_attention`` reads — ``(qe)-[:RESULTED_IN]->(o:DockingResult)``,
``o.outcome_type`` and ``o.community_id`` copied from the candidate — and only
for the named query entity. Nodes are written by ``handle_sync``; raw Cypher
only reads the result back and wipes the database.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import handle_outcomes, handle_sync, init_dependencies
from engine.state import get_state
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

TENANT = "it-outcome-preference"
DIRECTION = "window_to_depot"


def _write_pack(root: Path) -> None:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())
    raw["domain"]["id"] = TENANT
    nodes = {n["label"]: n for n in raw["ontology"]["nodes"]}
    nodes["OrbitalDepot"]["properties"].append({"name": "community_id", "type": "int"})
    nodes["LaunchWindow"]["managedby"] = "sync"
    raw["sync"]["endpoints"].append(
        {"path": "/v1/sync/launch-windows", "method": "POST", "batchstrategy": "UNWINDMERGE"}
        | {"targetnode": "LaunchWindow", "idproperty": "window_id"}
    )
    raw["scoring"]["dimensions"].append(
        {
            "name": "launch_preference",
            "source": "computed",
            "computation": "preference_attention",
            "candidateprop": "community_id",
            "weightkey": "w_preference",
            "defaultweight": 0.1,
            "metadata": {"success_value": "success", "success_property": "outcome_type"},
        }
    )
    (root / TENANT).mkdir()
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))


async def _wipe(driver: GraphDriver) -> None:
    await driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)


@pytest_asyncio.fixture
async def graph(
    graph_driver: GraphDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[GraphDriver, None]:
    monkeypatch.setattr(settings, "outcome_preference_history_enabled", True)
    _write_pack(tmp_path)
    await graph_driver.execute_query(f"CREATE DATABASE `{TENANT}` IF NOT EXISTS WAIT", database="system")
    await _wipe(graph_driver)
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))
    depots = [
        {"depot_id": "depot-a", "accepts_hazmat": True, "docking_ports": 4, "reliability": 0.9, "community_id": 1},
        {"depot_id": "depot-b", "accepts_hazmat": True, "docking_ports": 4, "reliability": 0.9, "community_id": 2},
    ]
    assert (await handle_sync(TENANT, {"entity_type": "orbital-depots", "batch": depots}))["status"] == "success"
    windows = [{"window_id": "window-1"}, {"window_id": "window-2"}]
    assert (await handle_sync(TENANT, {"entity_type": "launch-windows", "batch": windows}))["status"] == "success"
    yield graph_driver
    await _wipe(graph_driver)
    get_state().reset()


async def _record(window: str, depot: str, outcome: str, **extra: Any) -> dict[str, Any]:
    return await handle_outcomes(
        TENANT,
        {
            "match_id": f"{window}:{depot}",
            "candidate_id": depot,
            "outcome": outcome,
            "fingerprint": {"match_direction": DIRECTION},
            "query_entity_id": window,
            **extra,
        },
    )


async def _history(driver: GraphDriver) -> list[dict[str, Any]]:
    return await driver.execute_query(
        "MATCH (qe:LaunchWindow)-[:RESULTED_IN]->(o:DockingResult) "
        "RETURN qe.window_id AS window, o.candidate_id AS depot, o.outcome_type AS outcome_type, "
        "o.community_id AS community ORDER BY window, depot",
        database=TENANT,
    )


async def test_each_outcome_is_read_back_from_its_own_query_entity(graph: GraphDriver) -> None:
    first = await _record("window-1", "depot-a", "success")
    await _record("window-1", "depot-b", "failure")
    await _record("window-2", "depot-b", "success")

    assert first["preference_history"] == {"query_entity_id": "window-1", "linked_dimensions": ["launch_preference"]}
    assert await _history(graph) == [
        {"window": "window-1", "depot": "depot-a", "outcome_type": "success", "community": 1},
        {"window": "window-1", "depot": "depot-b", "outcome_type": "failure", "community": 2},
        {"window": "window-2", "depot": "depot-b", "outcome_type": "success", "community": 2},
    ]
    # The candidate's own RESULTED_IN edge is still written beside it.
    [row] = await graph.execute_query(
        "MATCH (:OrbitalDepot {depot_id: 'depot-a'})-[:RESULTED_IN]->(o:DockingResult) RETURN count(o) AS n",
        database=TENANT,
    )
    assert row["n"] == 1


async def test_a_replayed_outcome_links_its_query_entity_once(graph: GraphDriver) -> None:
    await _record("window-1", "depot-a", "success", outcome_id="out-replay-1")
    replay = await _record("window-1", "depot-a", "success", outcome_id="out-replay-1")
    assert replay["replayed"] is True
    assert len(await _history(graph)) == 1


async def test_an_unknown_query_entity_links_nothing(graph: GraphDriver) -> None:
    result = await _record("window-9", "depot-a", "success")
    assert result["status"] == "recorded"
    assert result["preference_history"]["linked_dimensions"] == []
    assert await _history(graph) == []


async def test_flag_off_no_outcome_is_reachable_from_a_query_entity(
    graph: GraphDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "outcome_preference_history_enabled", False)
    await _record("window-1", "depot-a", "success")
    assert await _history(graph) == []  # the defect: preference_attention could never read it


async def test_a_replay_naming_another_query_entity_is_refused_and_changes_nothing(graph: GraphDriver) -> None:
    from engine.handlers import ValidationError

    await _record("window-1", "depot-a", "success", outcome_id="out-bound-1")
    with pytest.raises(ValidationError, match="already recorded for another query entity"):
        await _record("window-2", "depot-a", "success", outcome_id="out-bound-1")
    assert await _history(graph) == [
        {"window": "window-1", "depot": "depot-a", "outcome_type": "success", "community": 1},
    ]


async def test_a_replay_keeps_the_community_recorded_with_the_outcome(graph: GraphDriver) -> None:
    await _record("window-1", "depot-a", "success", outcome_id="out-snap-1")
    moved = {"depot_id": "depot-a", "accepts_hazmat": True, "docking_ports": 4, "reliability": 0.9, "community_id": 7}
    await handle_sync(TENANT, {"entity_type": "orbital-depots", "batch": [moved]})
    await _record("window-1", "depot-a", "success", outcome_id="out-snap-1")
    [row] = await _history(graph)
    assert row["community"] == 1
