"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, contract, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

Typed sync contract — every layer that reads a sync row derives its behaviour
from the spec's declared shapes:

* generator — only declared, storable properties reach ``SET``; a
  ``list<scalar>`` taxonomy field links one node per element; a static child
  is written on create only;
* ingress — ``handle_sync`` refuses a mistyped batch before compliance or any
  Cypher, listing every defect; undeclared keys are dropped or refused.

Proven on the synthetic ``orbital-logistics`` pack and on PlasticOS. Schema and
admission: ``tests/unit/test_typed_spec_shapes.py``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.handlers import SyncRowRejectedError, ValidationError, handle_sync, init_dependencies
from engine.state import get_state
from engine.sync.generator import SyncGenerator
from engine.sync.row_contract import SyncRowContract
from tests.fixtures import typed_sync as fx

pytestmark = pytest.mark.unit

# ── Generator: declared projection ─────────────────────────────────────────────


def test_orbital_cypher_writes_only_declared_shapes() -> None:
    spec = fx.spec()
    cypher = SyncGenerator(spec).generate_sync_query(fx.endpoint(spec, fx.DEPOTS), [])
    assert cypher.splitlines() == [
        "UNWIND $batch AS row",
        "MERGE (n:OrbitalDepot {depot_id: row.node.depot_id})",
        "SET n += row.node, n._tenant = $tenant",
        "WITH n, row",
        "OPTIONAL MATCH (tax:OrbitClass) WHERE tax.class_code IN coalesce(row.node.served_orbits, [])",
        "WITH n, row, collect(tax) AS taxonomy_nodes",
        "FOREACH (tax IN taxonomy_nodes | MERGE (n)-[:SERVES_ORBIT]->(tax))",
        "WITH n, row",
        "FOREACH (child_row IN coalesce(row.children.stocked_grades, []) |",
        "  MERGE (c:PropellantGrade {grade_code: child_row.grade_code})",
        "  SET c += child_row, c._tenant = $tenant",
        "  MERGE (n)-[:STOCKS]->(c)",
        ")",
    ]


def test_a_static_child_is_written_on_create_only() -> None:
    spec = fx.plasticos()
    cypher = SyncGenerator(spec).generate_sync_query(fx.endpoint(spec, "/v1/sync/facilities"), [])
    assert (
        "  ON CREATE SET c += child_row{.*, line_id: null, direction: null, form: null}, c._tenant = $tenant" in cypher
    )
    assert "SET n += row{.*" not in cypher
    assert "SET n += row," not in cypher
    assert "MERGE (n)-[rel:PROCESSES {line_id: child_row.line_id}]->(c)" in cypher


def test_the_projection_keeps_only_writable_properties() -> None:
    spec = fx.spec()
    rows, dropped = SyncGenerator(spec).project_batch(
        fx.endpoint(spec, fx.DEPOTS),
        [
            {
                "depot_id": "d1",
                "reliability": None,
                "certifications": ["iso"],
                "served_orbits": ["LEO"],
                "colour": "teal",
                "stocked_grades": [{"grade_code": "LOX", "purity": 0.99, "batch": "b7"}],
            },
            {"depot_id": "d2", "colour": "red"},
        ],
    )
    assert rows == [
        {
            "node": {"depot_id": "d1", "reliability": None, "certifications": ["iso"], "served_orbits": ["LEO"]},
            "children": {"stocked_grades": [{"grade_code": "LOX", "purity": 0.99}]},
        },
        {"node": {"depot_id": "d2"}, "children": {"stocked_grades": []}},
    ]
    assert dropped == {"colour": 2, "stocked_grades[].batch": 1}


def test_an_undeclared_childsync_field_is_refused_by_the_generator() -> None:
    raw = fx.orbital_raw()
    fx.drop_depot_prop(raw, "stocked_grades")
    spec = fx.spec(raw)
    generator = SyncGenerator(spec)
    depots = fx.endpoint(spec, fx.DEPOTS)
    with pytest.raises(ValueError, match="childsync field 'stocked_grades' must be declared"):
        generator.generate_sync_query(depots, [])


def test_flag_off_restores_the_row_star_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_declared_projection_enabled", False)
    spec = fx.spec()
    cypher = SyncGenerator(spec).generate_sync_query(fx.endpoint(spec, fx.DEPOTS), [])
    assert "SET n += row{.*, stocked_grades: null}, n._tenant = $tenant" in cypher
    assert "row.node" not in cypher


# ── Ingress: the row contract ─────────────────────────────────────────────────


def _defects(rows: list[Any], *, refuse_undeclared: bool = False, spec: DomainSpec | None = None) -> list[str]:
    spec = spec or fx.spec()
    return SyncRowContract.from_spec(spec, fx.endpoint(spec, fx.DEPOTS)).defects(
        rows, refuse_undeclared=refuse_undeclared
    )


def test_a_conforming_row_has_no_defects() -> None:
    row = {
        "depot_id": "d1",
        "docking_ports": 4,
        "reliability": 1,
        "accepts_hazmat": True,
        "certifications": [],
        "served_orbits": ["LEO", "GEO"],
        "stocked_grades": [{"grade_code": "LOX", "purity": 0.5}, {"grade_code": "RP1", "purity": None}],
        "lat": None,
    }
    assert _defects([row]) == []


def test_every_defect_of_every_row_is_reported() -> None:
    rows = [
        {"depot_id": "d1", "stocked_grades": json.dumps([{"grade_code": "LOX"}])},
        {"depot_id": "d2", "stocked_grades": ["LOX", "RP1"]},
        {"depot_id": "d3", "reliability": "high", "accepts_hazmat": "yes", "docking_ports": 2.5},
        {"depot_id": "d4", "stocked_grades": [{"purity": "pure"}], "certifications": "iso"},
        {"depot_id": 5, "served_orbits": [None, 7]},
        {"reliability": True},
        "not-a-row",
    ]
    assert _defects(rows) == [
        "row 0 stocked_grades: expected list<map>, got string",
        "row 1 stocked_grades[0]: expected map, got string",
        "row 1 stocked_grades[1]: expected map, got string",
        "row 2 reliability: expected float, got string",
        "row 2 accepts_hazmat: expected bool, got string",
        "row 2 docking_ports: expected int, got float",
        "row 3 stocked_grades[0].grade_code: expected string (required), got missing",
        "row 3 stocked_grades[0].purity: expected float, got string",
        "row 3 certifications: expected list<string>, got string",
        "row 4 depot_id: expected string, got int",
        "row 4 served_orbits[0]: expected string, got null",
        "row 4 served_orbits[1]: expected string, got int",
        "row 5 depot_id: expected string (required id), got missing",
        "row 5 reliability: expected float, got bool",
        "row 6: expected map, got string",
    ]


def test_undeclared_keys_are_counted_or_refused() -> None:
    row = {"depot_id": "d1", "colour": "teal", "stocked_grades": [{"grade_code": "LOX", "batch": 7}]}
    assert _defects([row]) == []
    assert _defects([row], refuse_undeclared=True) == [
        "row 0 colour: expected no such declared property, got string",
        "row 0 stocked_grades[0].batch: expected no such declared property, got int",
    ]


def test_scalar_value_domains_are_checked_without_echoing_values() -> None:
    raw = fx.orbital_raw()
    fx.node_props(raw, "OrbitalDepot").extend(
        [
            {"name": "opened_at", "type": "datetime"},
            {"name": "opened_on", "type": "date"},
            {"name": "service_window", "type": "duration"},
            {"name": "tier", "type": "enum", "values": ["gold", "silver"]},
            {"name": "callsign", "type": "string", "nullable": False},
        ]
    )
    spec = fx.spec(raw)
    good = {
        "depot_id": "d1",
        "opened_at": "2026-11-11T09:00:00Z",
        "opened_on": "2026-11-11",
        "service_window": "PT36H",
        "tier": "gold",
        "callsign": "ALPHA",
    }
    assert _defects([good], spec=spec) == []
    bad = {
        "depot_id": "d1",
        "opened_at": "secret-yesterday",
        "opened_on": "11/11/2026",
        "service_window": "36 hours",
        "tier": "bronze",
        "callsign": None,
    }
    defects = _defects([bad], spec=spec)
    assert defects == [
        "row 0 opened_at: expected datetime, got string (not an ISO-8601 datetime)",
        "row 0 opened_on: expected date, got string (not an ISO-8601 date)",
        "row 0 service_window: expected duration, got string (not an ISO-8601 duration)",
        "row 0 tier: expected enum[gold|silver], got string (not a declared value)",
        "row 0 callsign: expected string (non-null), got null",
    ]
    assert not any("secret-yesterday" in defect or "bronze" in defect for defect in defects)


# ── Ingress: handle_sync ──────────────────────────────────────────────────────


@pytest.fixture
def driver() -> Iterator[MagicMock]:
    get_state().reset()
    mock = MagicMock()
    mock.execute_query = AsyncMock(return_value=[])
    mock.execute_write = AsyncMock()
    loader = MagicMock()
    loader.load_domain.return_value = fx.spec()
    init_dependencies(mock, loader)
    yield mock
    get_state().reset()


async def test_a_mistyped_batch_is_refused_before_any_graph_access(driver: MagicMock) -> None:
    batch = [
        {"depot_id": "d1", "stocked_grades": [{"grade_code": "LOX"}]},
        {"depot_id": "d2", "reliability": "high"},
        {"depot_id": "d3", "stocked_grades": '[{"grade_code": "LOX"}]'},
    ]
    with pytest.raises(SyncRowRejectedError) as refused:
        await handle_sync("orbital-logistics", {"entity_type": "orbital-depots", "batch": batch})
    assert refused.value.defects == [
        "row 1 reliability: expected float, got string",
        "row 2 stocked_grades: expected list<map>, got string",
    ]
    assert isinstance(refused.value, ValidationError)
    assert str(refused.value) == (
        "Sync refused, nothing written: 2 defect(s): row 1 reliability: expected float, got string; "
        "row 2 stocked_grades: expected list<map>, got string"
    )
    driver.execute_query.assert_not_awaited()
    driver.execute_write.assert_not_awaited()


async def test_a_conforming_batch_is_written_in_projected_shape(driver: MagicMock) -> None:
    batch = [{"depot_id": "d1", "colour": "teal", "served_orbits": ["LEO"], "stocked_grades": [{"grade_code": "LOX"}]}]
    result = await handle_sync("orbital-logistics", {"entity_type": "orbital-depots", "batch": batch})
    assert result == {
        "status": "success",
        "entity_type": "orbital-depots",
        "synced_count": 1,
        "undeclared_properties": {"colour": 1},
    }
    parameters = driver.execute_query.await_args.kwargs["parameters"]
    assert parameters["batch"] == [
        {
            "node": {"depot_id": "d1", "served_orbits": ["LEO"]},
            "children": {"stocked_grades": [{"grade_code": "LOX"}]},
        }
    ]


async def test_undeclared_refusal_flag_refuses_the_batch(driver: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_undeclared_property_refusal_enabled", True)
    with pytest.raises(SyncRowRejectedError, match="row 0 colour: expected no such declared property, got string"):
        await handle_sync(
            "orbital-logistics", {"entity_type": "orbital-depots", "batch": [{"depot_id": "d1", "colour": "teal"}]}
        )
    driver.execute_query.assert_not_awaited()


async def test_validation_flag_off_leaves_typing_to_the_database(
    driver: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "sync_row_type_validation_enabled", False)
    result = await handle_sync(
        "orbital-logistics", {"entity_type": "orbital-depots", "batch": [{"depot_id": "d1", "reliability": "high"}]}
    )
    assert result["synced_count"] == 1
    driver.execute_query.assert_awaited_once()


async def test_an_endpoint_the_spec_does_not_declare_is_refused_at_ingress(driver: MagicMock) -> None:
    raw = fx.orbital_raw()
    fx.drop_depot_prop(raw, "stocked_grades")
    raw_endpoint = copy.deepcopy(fx.depot_endpoint(raw))
    assert raw_endpoint["childsync"]
    loader = MagicMock()
    loader.load_domain.return_value = fx.spec(raw)
    init_dependencies(driver, loader)
    with pytest.raises(ValidationError, match="the endpoint's fields are not declared by the spec"):
        await handle_sync("orbital-logistics", {"entity_type": "orbital-depots", "batch": [{"depot_id": "d1"}]})
    driver.execute_query.assert_not_awaited()
