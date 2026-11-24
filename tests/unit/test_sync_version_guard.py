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
    assert lines[:10] == [
        "UNWIND $batch AS row",
        "MERGE (n:Station {station_id: row.node.station_id})",
        "SET n._tenant = $tenant",
        "WITH n, row",
        "CALL {",
        "  WITH n, row",
        f"  {STALE}",
        "  CALL {",
        "    WITH n, row, stale_write",
        "    WITH n, row, stale_write WHERE NOT stale_write",
    ]
    assert lines[-4:] == [
        "  }",
        "  RETURN stale_write",
        "}",
        "RETURN count(CASE WHEN stale_write THEN 1 END) AS stale_count",
    ]
    body = lines[10:-4]
    assert body[0] == "    WITH n, row, row.node AS node_row"
    assert body[1].startswith("    SET n += node_row{.*, ")
    # An absent or null version never clears the stored one.
    assert "sync_version: coalesce(node_row.sync_version, n.sync_version)" in body[1]
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
    assert lines[2:4] == ["SET n._tenant = $tenant", "WITH n, row"]
    assert f"  {STALE}" in lines
    assert "n.sync_version = coalesce(row.node.sync_version, n.sync_version)" in "\n".join(lines)
    assert lines[-1] == (
        "RETURN count(CASE WHEN NOT stale_write THEN 1 END) AS synced_count, "
        "count(CASE WHEN stale_write THEN 1 END) AS stale_count"
    )


def test_a_listed_sync_version_is_still_kept_not_nulled(precedence_on: None) -> None:
    raw = _raw()
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/station-bands",
            "targetnode": "Station",
            "idproperty": "station_id",
            "batchstrategy": "UNWINDMATCHSET",
            "fieldsupdated": ["band", "sync_version"],
        }
    )
    cypher = _cypher(raw, "/v1/sync/station-bands")
    assert "n.sync_version = row.node.sync_version" not in cypher
    assert cypher.count("n.sync_version = coalesce(row.node.sync_version, n.sync_version)") == 1


def test_a_versioned_patch_without_fieldsupdated_keeps_the_version(precedence_on: None) -> None:
    raw = _raw()
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/station-patch",
            "targetnode": "Station",
            "idproperty": "station_id",
            "batchstrategy": "UNWINDMATCHSET",
        }
    )
    cypher = _cypher(raw, "/v1/sync/station-patch")
    assert "sync_version: coalesce(node_row.sync_version, n.sync_version)" in cypher


def test_ingress_refuses_a_versioned_row_without_a_version(precedence_on: None) -> None:
    from engine.sync.row_contract import SyncRowContract

    spec = DomainSpec(**_raw())
    endpoint = next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == "/v1/sync/stations")
    contract = SyncRowContract.from_spec(spec, endpoint)
    rows = [
        {"station_id": "a", "title": "a", "sync_version": 3},
        {"station_id": "b", "title": "b"},
        {"station_id": "c", "title": "c", "sync_version": None},
    ]
    defects = contract.defects(rows, refuse_undeclared=False)
    assert [defect for defect in defects if "sync_version" in defect] == [
        "row 1 sync_version: expected int (required by the stale-write guard), got missing",
        "row 2 sync_version: expected int (required by the stale-write guard), got null",
    ]


def test_ingress_accepts_an_unversioned_row_with_the_guard_off() -> None:
    from engine.sync.row_contract import SyncRowContract

    spec = DomainSpec(**_raw())
    endpoint = next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == "/v1/sync/stations")
    contract = SyncRowContract.from_spec(spec, endpoint)
    assert contract.defects([{"station_id": "b", "title": "b"}], refuse_undeclared=False) == []
