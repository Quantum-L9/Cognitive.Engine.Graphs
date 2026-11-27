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

from collections.abc import AsyncGenerator, Callable
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


def _write_pack(root: Path, variant: Callable[[dict[str, Any]], None] | None = None) -> None:
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
    # #567's strict validation: a preference_attention dimension needs a traversal step
    # that binds `qe`, here the queryentity label the producer links outcomes from.
    raw["ontology"]["edges"].append(
        {
            "type": "CONSIDERED",
            "from": "LaunchWindow",
            "to": "OrbitalDepot",
            "direction": "DIRECTED",
            "category": "transaction",
            "managedby": "api",
        }
    )
    raw["traversal"] = {
        "steps": [
            {
                "name": "bind_query_window",
                "pattern": "(qe:LaunchWindow)-[:CONSIDERED]->(candidate:OrbitalDepot)",
                "required": False,
                "matchdirections": [DIRECTION],
            }
        ]
    }
    if variant is not None:
        variant(raw)
    (root / TENANT).mkdir(parents=True)
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))


RELAY = "relay_to_depot"


def _relay_direction(raw: dict[str, Any]) -> None:
    """CEG-04: a second direction whose query entity shares the local id property and values."""
    raw["ontology"]["nodes"].append(
        {
            "label": "RelayWindow",
            "managedby": "sync",
            "queryentity": True,
            "matchdirection": RELAY,
            "properties": [{"name": "window_id", "type": "string", "required": True}],
        }
    )
    raw["matchentities"]["candidate"].append({"label": "OrbitalDepot", "matchdirection": RELAY})
    raw["matchentities"]["queryentity"].append({"label": "RelayWindow", "matchdirection": RELAY})
    raw["queryschema"]["matchdirections"].append(RELAY)
    raw["sync"]["endpoints"].append(
        {"path": "/v1/sync/relay-windows", "method": "POST", "batchstrategy": "UNWINDMERGE"}
        | {"targetnode": "RelayWindow", "idproperty": "window_id"}
    )
    raw["ontology"]["edges"].append(
        {
            "type": "CONSIDERED_RELAY",
            "from": "RelayWindow",
            "to": "OrbitalDepot",
            "direction": "DIRECTED",
            "category": "transaction",
            "managedby": "api",
        }
    )
    raw["traversal"]["steps"].append(
        {
            "name": "bind_query_relay",
            "pattern": "(qe:RelayWindow)-[:CONSIDERED_RELAY]->(candidate:OrbitalDepot)",
            "required": False,
            "matchdirections": [RELAY],
        }
    )
    window_dim = next(d for d in raw["scoring"]["dimensions"] if d["name"] == "launch_preference")
    window_dim["matchdirections"] = [DIRECTION]
    raw["scoring"]["dimensions"].append(
        {**window_dim, "name": "relay_preference", "weightkey": "w_relay", "matchdirections": [RELAY]}
    )


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
        # Same match identity, only the query entity changes.
        await _record("window-2", "depot-a", "success", outcome_id="out-bound-1", match_id="window-1:depot-a")
    assert await _history(graph) == [
        {"window": "window-1", "depot": "depot-a", "outcome_type": "success", "community": 1},
    ]


async def test_a_replay_cannot_add_a_query_entity_to_an_outcome_recorded_without_one(graph: GraphDriver) -> None:
    from engine.handlers import ValidationError

    async def _counts() -> dict[str, Any]:
        [row] = await graph.execute_query(
            "MATCH (o:DockingResult {outcome_id: 'out-unbound-1'}) "
            "OPTIONAL MATCH (qe:LaunchWindow)-[r:RESULTED_IN]->(o) "
            "RETURN count(DISTINCT o) AS outcomes, count(r) AS links, o.query_entity_id AS bound, "
            "o.outcome_type AS outcome_type",
            database=TENANT,
        )
        return row

    first = {"outcome_id": "out-unbound-1", "match_id": "window-1:depot-a", "query_entity_id": None}
    await _record("window-1", "depot-a", "success", **first)
    before = await _counts()
    assert before == {"outcomes": 1, "links": 0, "bound": None, "outcome_type": None}

    with pytest.raises(ValidationError, match="recorded without a query entity"):
        await _record("window-2", "depot-a", "success", **first | {"query_entity_id": "window-2"})
    assert await _counts() == before
    assert await _history(graph) == []

    replay = await _record("window-1", "depot-a", "success", **first)
    assert "preference_history" not in replay
    assert await _counts() == before


async def test_a_replay_keeps_the_community_recorded_with_the_outcome(graph: GraphDriver) -> None:
    await _record("window-1", "depot-a", "success", outcome_id="out-snap-1")
    moved = {"depot_id": "depot-a", "accepts_hazmat": True, "docking_ports": 4, "reliability": 0.9, "community_id": 7}
    await handle_sync(TENANT, {"entity_type": "orbital-depots", "batch": [moved]})
    await _record("window-1", "depot-a", "success", outcome_id="out-snap-1")
    [row] = await _history(graph)
    assert row["community"] == 1


def _reload(graph: GraphDriver, root: Path, variant: Callable[[dict[str, Any]], None]) -> None:
    _write_pack(root, variant)
    get_state().reset()
    init_dependencies(graph, DomainPackLoader(config_path=str(root)))


def _window_dim(raw: dict[str, Any]) -> dict[str, Any]:
    return next(d for d in raw["scoring"]["dimensions"] if d["name"] == "launch_preference")


def _by_region(raw: dict[str, Any], **metadata: Any) -> None:
    depot = next(n for n in raw["ontology"]["nodes"] if n["label"] == "OrbitalDepot")
    depot["properties"].append({"name": "region_id", "type": "int"})
    raw["scoring"]["dimensions"].append(
        {**_window_dim(raw), "name": "by_region", "candidateprop": "region_id", "weightkey": "w_region"}
        | {"defaultweight": 0.0, "metadata": {"success_value": "won", "success_property": "region_outcome", **metadata}}
    )


async def _outcomes(graph: GraphDriver) -> int:
    [row] = await graph.execute_query("MATCH (o:DockingResult) RETURN count(o) AS n", database=TENANT)
    return int(row["n"])


async def test_ceg03_a_success_property_the_outcome_owns_is_refused_and_writes_nothing(
    graph: GraphDriver, tmp_path: Path
) -> None:
    from engine.handlers import ValidationError

    _reload(graph, tmp_path / "owned", lambda raw: _window_dim(raw)["metadata"].update(success_property="created_at"))
    with pytest.raises(ValidationError, match="outcome identity field"):
        await _record("window-1", "depot-a", "success", outcome_id="out-owned-1")
    assert await _outcomes(graph) == 0


async def test_ceg03_a_community_property_another_dimension_stores_success_in_is_refused(
    graph: GraphDriver, tmp_path: Path
) -> None:
    from engine.handlers import ValidationError

    _reload(graph, tmp_path / "collide", lambda raw: _by_region(raw, success_property="community_id"))
    with pytest.raises(ValidationError, match="would both write it"):
        await _record("window-1", "depot-a", "success", outcome_id="out-collide-1")
    assert await _outcomes(graph) == 0


async def test_ceg03_two_dimensions_with_disjoint_write_sets_both_record(graph: GraphDriver, tmp_path: Path) -> None:
    _reload(graph, tmp_path / "disjoint", _by_region)
    depot = {"depot_id": "depot-a", "accepts_hazmat": True, "docking_ports": 4, "reliability": 0.9}
    synced = await handle_sync(
        TENANT, {"entity_type": "orbital-depots", "batch": [depot | {"community_id": 1, "region_id": 7}]}
    )
    assert synced["status"] == "success"

    result = await _record("window-1", "depot-a", "success", outcome_id="out-disjoint-1")

    assert result["preference_history"]["linked_dimensions"] == ["launch_preference", "by_region"]
    [row] = await graph.execute_query(
        "MATCH (o:DockingResult {outcome_id: 'out-disjoint-1'}) "
        "RETURN o.outcome_type AS outcome_type, o.region_outcome AS region_outcome, "
        "o.community_id AS community, o.region_id AS region, o.outcome AS outcome",
        database=TENANT,
    )
    assert row == {
        "outcome_type": "success",
        "region_outcome": "won",
        "community": 1,
        "region": 7,
        "outcome": "success",
    }


async def test_ceg04_a_replay_resolving_another_query_entity_with_the_same_id_is_refused(
    graph: GraphDriver, tmp_path: Path
) -> None:
    from engine.handlers import ValidationError

    _reload(graph, tmp_path / "relay", _relay_direction)
    synced = await handle_sync(TENANT, {"entity_type": "relay-windows", "batch": [{"window_id": "window-1"}]})
    assert synced["status"] == "success"
    first = await _record("window-1", "depot-a", "success", outcome_id="out-relay-1")
    assert first["preference_history"]["linked_dimensions"] == ["launch_preference"]

    async def _bindings() -> list[dict[str, Any]]:
        return await graph.execute_query(
            "MATCH (o:DockingResult {outcome_id: 'out-relay-1'}) "
            "OPTIONAL MATCH (qe)-[r]->(o) WHERE qe:LaunchWindow OR qe:RelayWindow "
            "RETURN [l IN labels(qe) WHERE l IN ['LaunchWindow', 'RelayWindow']] AS labels, count(r) AS links, "
            "o.query_entity_id AS bound_id, o.query_entity_label AS bound_label",
            database=TENANT,
        )

    before = await _bindings()
    assert before == [{"labels": ["LaunchWindow"], "links": 1, "bound_id": "window-1", "bound_label": "LaunchWindow"}]
    # Same local id, same match identity, another direction: another query entity.
    with pytest.raises(ValidationError, match="already recorded for another query entity"):
        await _record(
            "window-1", "depot-a", "success", outcome_id="out-relay-1", fingerprint={"match_direction": RELAY}
        )
    assert await _bindings() == before
