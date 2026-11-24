"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, provenance, stale-write]
owner: engine-team
status: active
--- /L9_META ---

Stale-write guard (#351 guardrail, EIE #419 rule 6).

A node sync target that declares ``sync_version: int`` carries a per-subject,
strictly increasing version on each row. With
``sync_provenance_precedence_enabled`` the sync Cypher locks the node, compares
the row's version with the stored one and writes the row (node, taxonomy and
childsync) only when it is not lower, all in one statement. The behaviour on
Neo4j is in ``tests/integration/test_sync_version_guard.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.sync.generator import SyncGenerator
from engine.sync.row_contract import endpoint_shape_defects

pytestmark = pytest.mark.unit

SYNTHNEUTRAL = Path(__file__).resolve().parents[1] / "fixtures" / "test_only_packs" / "synthneutral" / "spec.yaml"
STALE = "WITH n, row, coalesce(row.node.sync_version < n.sync_version, false) AS stale_write"


@pytest.fixture
def precedence_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)


def _raw(version_type: str | None = "int") -> dict[str, Any]:
    raw = yaml.safe_load(SYNTHNEUTRAL.read_text())
    station = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Station")
    station["properties"] = [prop for prop in station["properties"] if prop["name"] != "sync_version"]
    if version_type is not None:
        station["properties"].append({"name": "sync_version", "type": version_type})
    return raw


def _cypher(raw: dict[str, Any], path: str = "/v1/sync/stations") -> str:
    spec = DomainSpec(**raw)
    endpoint = next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == path)
    return SyncGenerator(spec).generate_sync_query(endpoint, [])


def test_a_versioned_merge_locks_compares_and_writes_in_one_statement(precedence_on: None) -> None:
    lines = _cypher(_raw()).splitlines()
    assert lines[:7] == [
        "UNWIND $batch AS row",
        "MERGE (n:Station {station_id: row.node.station_id})",
        "SET n._tenant = $tenant",
        STALE,
        "CALL {",
        "  WITH n, row, stale_write",
        "  WITH n, row, stale_write WHERE NOT stale_write",
    ]
    assert lines[-2:] == ["}", "RETURN count(CASE WHEN stale_write THEN 1 END) AS stale_count"]
    body = lines[7:-2]
    assert body[0] == "  WITH n, row, row.node AS node_row"
    assert body[1].startswith("  SET n += node_row{.*, ")
    # The childsync line write sits inside the guarded subquery: a stale row writes no item.
    assert any(line.strip().startswith("MERGE (n)-[rel:HANDLES") for line in body)


def test_an_unversioned_pack_keeps_todays_statement(precedence_on: None) -> None:
    cypher = _cypher(_raw(version_type=None))
    assert "stale_write" not in cypher
    assert "CALL {" not in cypher


def test_the_guard_is_off_with_precedence_off() -> None:
    cypher = _cypher(_raw())
    assert "stale_write" not in cypher
    assert cypher.splitlines()[2] == "SET n += row.node, n._tenant = $tenant"


def test_a_non_int_sync_version_is_refused_at_admission(precedence_on: None) -> None:
    spec = DomainSpec(**_raw(version_type="string"))
    endpoint = next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == "/v1/sync/stations")
    assert endpoint_shape_defects(spec, endpoint) == [
        "sync /v1/sync/stations: Station property 'sync_version' must be declared int, found string"
    ]


def test_a_versioned_match_set_counts_synced_and_stale(precedence_on: None) -> None:
    raw = _raw()
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/station-bands",
            "targetnode": "Station",
            "idproperty": "station_id",
            "batchstrategy": "UNWINDMATCHSET",
            "fieldsupdated": ["band"],
        }
    )
    lines = _cypher(raw, "/v1/sync/station-bands").splitlines()
    assert lines[1] == "MATCH (n:Station {station_id: row.node.station_id})"
    assert lines[2:4] == ["SET n._tenant = $tenant", STALE]
    assert "n.sync_version = coalesce(row.node.sync_version, n.sync_version)" in "\n".join(lines)
    assert lines[-1] == (
        "RETURN count(CASE WHEN NOT stale_write THEN 1 END) AS synced_count, "
        "count(CASE WHEN stale_write THEN 1 END) AS stale_count"
    )
