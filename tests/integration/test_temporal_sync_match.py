"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, sync, match, temporal, egress]
owner: engine-team
status: active
--- /L9_META ---

Declared temporals end to end on real Neo4j (ceg-scoring-audit L1, D1).

Every graph state here is written by the engine's own ``handle_sync`` and read
by ``handle_match`` / ``chassis.actions.execute_action`` (the call path the
chassis uses, which runs ``deflate_egress``). No state is seeded with raw Cypher;
raw Cypher only reads back what the engine stored.

* ND-TEMPORAL-SYNC: a synced ``last_transaction_date`` is stored as a native
  ``DateTime`` and the buyer match scores it (it used to be a string the
  compiled ``duration.inDays`` refused).
* ND-SERIAL: the match response — the native temporal and the ``updated_at``
  an UNWINDMATCHSET sync stamps with ``datetime()`` — survives egress.
* Freshness: a gate on the native property admits a fresh synced candidate and
  excludes a stale one.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import yaml

from engine.config.schema import DomainSpec
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

PLASTICOS = "plasticos"
BUYER = "supply_opportunity_to_buyer_facility"
ACTIVITY = "facility-activity"


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


def _facility(facility_id: str, days_ago: int | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "facility_id": facility_id,
        "name": f"Facility {facility_id}",
        "contamination_tolerance": 0.10,
        "direction": "in",
        "form": "PELLET",
        "processed_polymers": [{"code": "PET", "line_id": f"{facility_id}-pet"}],
    }
    if days_ago is not None:
        row["last_transaction_date"] = _iso(days_ago)
    return row


def _match_payload() -> dict[str, Any]:
    return {"query": {"polymer_type": "PET", "contamination_pct": 0.04}, "match_direction": BUYER, "top_n": 10}


def _by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate"]["facility_id"]: row for row in result.get("candidates", [])}


def _variant_spec() -> DomainSpec:
    """The committed plasticos pack plus an UNWINDMATCHSET endpoint and a freshness gate.

    Both are declarations the schema already owns; the pack itself declares
    neither, so the test adds them to its own copy (nothing on disk changes).
    """
    raw = yaml.safe_load((DESIGN_PACKS_ROOT / PLASTICOS / "spec.yaml").read_text())
    raw["sync"]["endpoints"].append(
        {
            "path": f"/v1/sync/{ACTIVITY}",
            "targetnode": "Facility",
            "idproperty": "facility_id",
            "batchstrategy": "UNWINDMATCHSET",
            "fieldsupdated": ["last_transaction_date"],
        }
    )
    raw["gates"].append(
        {
            "name": "recent_transaction",
            "type": "freshness",
            "candidateprop": "last_transaction_date",
            "maxagedays": 90,
            "nullbehavior": "fail",
            "matchdirections": [BUYER],
        }
    )
    return DomainSpec.model_validate(raw)


class _VariantLoader:
    """The design-pack loader, serving the variant spec for plasticos."""

    def __init__(self, inner: Any, spec: DomainSpec) -> None:
        self._inner = inner
        self._spec = spec

    def load_domain(self, domain_id: str) -> DomainSpec:
        return self._spec if domain_id == PLASTICOS else self._inner.load_domain(domain_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest_asyncio.fixture
async def variant_deps(graph_driver, design_pack_loader, plasticos_database) -> AsyncGenerator[DomainSpec, None]:
    from engine.config.loader import admission_defects
    from engine.handlers import init_dependencies
    from engine.state import get_state

    spec = _variant_spec()
    assert admission_defects(spec) == []  # the variant is an admissible pack, not a bypass
    get_state().reset()
    init_dependencies(graph_driver, _VariantLoader(design_pack_loader, spec))
    yield spec
    get_state().reset()
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=PLASTICOS)


async def _stored_type(graph_driver, facility_id: str, prop: str) -> str:
    rows = await graph_driver.execute_query(
        f"MATCH (f:Facility {{facility_id: $id}}) RETURN valueType(f.{prop}) AS t",
        parameters={"id": facility_id},
        database=PLASTICOS,
    )
    return str(rows[0]["t"])


@pytest.mark.integration
async def test_synced_datetime_is_native_and_the_buyer_match_scores_it(design_pack_deps, graph_driver):
    from chassis.actions import execute_action
    from engine.handlers import handle_match, handle_sync

    synced = await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": [_facility("dated", 30)]})
    assert synced["status"] == "success"
    assert await _stored_type(graph_driver, "dated", "last_transaction_date") == "ZONED DATETIME NOT NULL"

    result = await handle_match(PLASTICOS, _match_payload())
    row = _by_id(result)["dated"]
    # Legacy temporalproximity (FEATURE_GATES §37 off): 0.6 * exp(-days / 180) + 0 + 0.
    assert row["dimension_scores"]["recency"] == pytest.approx(0.6 * math.exp(-30 / 180), abs=1e-6)

    # The chassis path: handler -> deflate_egress -> the dict the HTTP layer serialises.
    envelope = await execute_action("match", _match_payload(), PLASTICOS, "trace-temporal-1")
    assert envelope["status"] == "success"
    wire = json.loads(json.dumps(envelope["data"]))
    dated = _by_id(wire)["dated"]
    assert datetime.fromisoformat(dated["candidate"]["last_transaction_date"]) == datetime.fromisoformat(
        _facility("dated", 30)["last_transaction_date"]
    )


@pytest.mark.integration
async def test_a_naive_datetime_is_refused_and_nothing_is_written(design_pack_deps, graph_driver):
    from engine.handlers import SyncRowRejectedError, handle_sync

    row = _facility("naive", None)
    row["last_transaction_date"] = "2026-11-20T12:00:00"
    with pytest.raises(SyncRowRejectedError, match="timezone-naive"):
        await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": [row]})
    rows = await graph_driver.execute_query(
        "MATCH (f:Facility {facility_id: 'naive'}) RETURN count(f) AS n", database=PLASTICOS
    )
    assert rows[0]["n"] == 0


@pytest.mark.integration
async def test_freshness_gate_and_matchset_updated_at_survive_egress(variant_deps, graph_driver):
    from chassis.actions import execute_action
    from engine.handlers import handle_match, handle_sync

    batch = [_facility("fresh", 10), _facility("stale", 200)]
    assert (await handle_sync(PLASTICOS, {"entity_type": "facilities", "batch": batch}))["status"] == "success"

    before = _by_id(await handle_match(PLASTICOS, _match_payload()))
    assert set(before) == {"fresh"}  # 200 days > maxagedays 90: excluded; 10 days admitted

    # UNWINDMATCHSET with fieldsupdated: SET n.last_transaction_date = ..., n.updated_at = datetime().
    update = await handle_sync(
        PLASTICOS,
        {"entity_type": ACTIVITY, "batch": [{"facility_id": "stale", "last_transaction_date": _iso(5)}]},
    )
    assert update["synced_count"] == 1
    assert await _stored_type(graph_driver, "stale", "last_transaction_date") == "ZONED DATETIME NOT NULL"
    assert await _stored_type(graph_driver, "stale", "updated_at") == "ZONED DATETIME NOT NULL"

    after = _by_id(await handle_match(PLASTICOS, _match_payload()))
    assert set(after) == {"fresh", "stale"}
    assert after["stale"]["dimension_scores"]["recency"] == pytest.approx(0.6 * math.exp(-5 / 180), abs=1e-6)

    envelope = await execute_action("match", _match_payload(), PLASTICOS, "trace-temporal-2")
    assert envelope["status"] == "success"
    wire = _by_id(json.loads(json.dumps(envelope["data"])))
    assert set(wire) == {"fresh", "stale"}
    # RETURN candidate carries every stored property, the stamped updated_at included.
    stamped = datetime.fromisoformat(wire["stale"]["candidate"]["updated_at"])  # ISO-8601, not a driver object
    assert stamped.tzinfo is not None
