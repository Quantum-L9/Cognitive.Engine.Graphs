"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, provenance, contract]
owner: engine-team
status: active
--- /L9_META ---

Value provenance precedence (#351 revision, 2026-11-24).

Naming agreed with EIE #419: a node row field ``F`` is declared beside
``F_provenance: enum[observed|enriched]``; a childsync ``list<map>`` item
declares one item-level ``provenance`` with the same enum, stored on the line
edge (or child node) that receives the item. With
``sync_provenance_precedence_enabled`` the sync Cypher resolves precedence in
the write itself:

* observed over enriched applies; enriched over a stored observed is refused;
* observed over observed (and enriched over enriched) applies;
* a null never clears; an absent provenance value counts as enriched;
* a field or item that declares no provenance stays in legacy mode, flag on or off.

These tests pin the generated Cypher and the admission of the declarations on
an inline fixture: the synthneutral pack with its provenance declared here. The
shipped pack declarations and the Neo4j execution are covered by the stacked
pack PR (``tests/unit/test_pack_provenance.py``,
``tests/integration/test_sync_provenance_precedence.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.config.schema import DomainSpec, PropertySpec, PropertyType
from engine.config.settings import settings
from engine.sync.generator import SyncGenerator
from engine.sync.row_contract import (
    ITEM_PROVENANCE,
    endpoint_shape_defects,
    item_provenance_declared,
    item_provenance_defects,
    provenance_companion,
    provenance_defects,
    provenance_governed,
)
from tests.fixtures import typed_sync as fx

pytestmark = pytest.mark.unit

TEST_ONLY_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "test_only_packs"
SYNTHNEUTRAL = TEST_ONLY_ROOT / "synthneutral" / "spec.yaml"

ENUM = {"type": "enum", "values": ["observed", "enriched"]}
DEMOTED = (
    "(coalesce(child_row.provenance, $provenance_enriched) = $provenance_enriched "
    "AND {stored}.provenance = $provenance_observed)"
)
KEEP_BAND = (
    "(node_row.band IS NULL OR (coalesce(node_row.band_provenance, $provenance_enriched) = "
    "$provenance_enriched AND n.band_provenance = $provenance_observed))"
)


@pytest.fixture
def precedence_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)


def _declared_raw() -> dict[str, Any]:
    """The synthneutral pack with provenance declared inline (idempotent over a pack that declares it)."""
    raw = yaml.safe_load(SYNTHNEUTRAL.read_text())
    station = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Station")

    def declare(properties: list[dict[str, Any]], name: str) -> None:
        if name not in {prop["name"] for prop in properties}:
            properties.append({"name": name, **ENUM})

    for field in ("title", "max_load", "band", "postal_code", "region"):
        declare(station["properties"], provenance_companion(field))
    items = next(prop for prop in station["properties"] if prop["name"] == "handled_kinds")["items"]["properties"]
    declare(items, ITEM_PROVENANCE)
    declare(next(edge for edge in raw["ontology"]["edges"] if edge["type"] == "HANDLES")["properties"], ITEM_PROVENANCE)
    child = raw["sync"]["endpoints"][0]["childsync"][0]
    if ITEM_PROVENANCE not in child["edgeproperties"]:
        child["edgeproperties"].append(ITEM_PROVENANCE)
    return raw


def _declared() -> DomainSpec:
    return DomainSpec(**_declared_raw())


def _endpoint(spec: DomainSpec, path: str) -> Any:
    return next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == path)


def _prov(name: str, values: list[str] | None = None) -> PropertySpec:
    return PropertySpec(
        name=provenance_companion(name), type=PropertyType.ENUM, values=values or ["observed", "enriched"]
    )


# ── declarations ──────────────────────────────────────────────────────────────


def test_a_property_is_governed_only_beside_its_enum_companion() -> None:
    props = [
        PropertySpec(name="band", type=PropertyType.STRING),
        _prov("band"),
        PropertySpec(name="tier", type=PropertyType.STRING),
        _prov("tier", ["observed", "inferred"]),
        PropertySpec(name="title", type=PropertyType.STRING),
    ]
    assert provenance_governed(props) == frozenset({"band"})


def test_a_malformed_companion_is_an_admission_defect() -> None:
    props = [
        PropertySpec(name="tier", type=PropertyType.STRING),
        _prov("tier", ["observed", "inferred"]),
        _prov("ghost"),
    ]
    defects = provenance_defects("Station", props)
    assert any("'tier_provenance'" in defect and "enum[enriched|observed]" in defect for defect in defects)
    assert any("'ghost_provenance'" in defect and "no storable property" in defect for defect in defects)


def test_admission_checks_companions_only_with_precedence_on(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _declared_raw()
    station = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Station")
    station["properties"].append({"name": "orphan_provenance", "type": "enum", "values": ["observed", "enriched"]})
    spec = DomainSpec(**raw)
    endpoint = _endpoint(spec, "/v1/sync/stations")
    assert endpoint_shape_defects(spec, endpoint) == []
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)
    assert endpoint_shape_defects(spec, endpoint) == [
        (
            "sync /v1/sync/stations: Station property 'orphan_provenance': provenance companion of no storable "
            "property declared beside it"
        )
    ]


def test_an_item_declares_one_provenance_not_per_field_companions() -> None:
    items = [
        PropertySpec(name="direction", type=PropertyType.STRING),
        PropertySpec(name="direction_provenance", type=PropertyType.ENUM, values=["observed", "enriched"]),
        PropertySpec(name=ITEM_PROVENANCE, type=PropertyType.ENUM, values=["observed", "inferred"]),
    ]
    assert not item_provenance_declared(items)
    defects = item_provenance_defects("handled_kinds items", items, field_companions=False)
    assert defects == [
        (
            "handled_kinds items property 'direction_provenance': an item declares one 'provenance', not a "
            "per-field companion"
        ),
        (
            "handled_kinds items property 'provenance': item provenance must be declared enum[enriched|observed], "
            "found enum[observed|inferred]"
        ),
    ]
    assert item_provenance_defects("Kind", items[:2], field_companions=True) == []


# ── generator ─────────────────────────────────────────────────────────────────


def test_precedence_off_leaves_the_cypher_unchanged() -> None:
    spec = _declared()
    cypher = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/stations"), [])
    assert "provenance_observed" not in cypher
    assert cypher.splitlines()[:3] == [
        "UNWIND $batch AS row",
        "MERGE (n:Station {station_id: row.node.station_id})",
        "SET n += row.node, n._tenant = $tenant",
    ]
    assert SyncGenerator(spec).query_parameters() == {}


def test_a_pack_with_no_provenance_declared_stays_legacy_with_the_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """EIE #419 compatibility: no declared provenance -> byte-identical Cypher, flag on or off."""
    spec = fx.spec()
    endpoints = [endpoint for endpoint in spec.sync.endpoints if endpoint.targetnode and not endpoint.targetedge]
    legacy = [SyncGenerator(spec).generate_sync_query(endpoint, []) for endpoint in endpoints]
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)
    assert [SyncGenerator(spec).generate_sync_query(endpoint, []) for endpoint in endpoints] == legacy
    assert all("provenance" not in cypher for cypher in legacy)


def test_an_undeclared_item_stays_legacy_beside_declared_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _declared_raw()
    station = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Station")
    items = next(prop for prop in station["properties"] if prop["name"] == "handled_kinds")["items"]["properties"]
    items[:] = [prop for prop in items if prop["name"] != ITEM_PROVENANCE]
    raw["sync"]["endpoints"][0]["childsync"][0]["edgeproperties"] = ["slot_id", "direction"]
    spec = DomainSpec(**raw)
    endpoint = _endpoint(spec, "/v1/sync/stations")
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)
    cypher = SyncGenerator(spec).generate_sync_query(endpoint, [])
    assert "SET rel.slot_id = child_row.slot_id, rel.direction = child_row.direction, rel._tenant = $tenant" in cypher
    assert "band: CASE WHEN" in cypher  # the declared row fields are still governed


def test_node_properties_resolve_precedence_in_the_set(precedence_on: None) -> None:
    spec = _declared()
    lines = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/stations"), []).splitlines()
    assert lines[:3] == [
        "UNWIND $batch AS row",
        "WITH row, row.node AS node_row",
        "MERGE (n:Station {station_id: node_row.station_id})",
    ]
    set_line = lines[3]
    assert set_line.startswith("SET n += node_row{.*, ")
    assert set_line.endswith("}, n._tenant = $tenant")
    assert f"band: CASE WHEN {KEEP_BAND} THEN n.band ELSE node_row.band END" in set_line
    assert (
        f"band_provenance: CASE WHEN {KEEP_BAND} THEN n.band_provenance "
        "ELSE coalesce(node_row.band_provenance, $provenance_enriched) END"
    ) in set_line
    for name in ("title", "max_load", "postal_code", "region"):
        assert f"{name}: CASE WHEN (node_row.{name} IS NULL" in set_line
    assert "station_id:" not in set_line  # the id is never governed
    assert SyncGenerator(spec).query_parameters() == {
        "provenance_observed": "observed",
        "provenance_enriched": "enriched",
    }


def test_line_edge_values_follow_the_item_provenance(precedence_on: None) -> None:
    spec = _declared()
    cypher = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/stations"), [])
    rel_set = next(line.strip() for line in cypher.splitlines() if line.strip().startswith("SET rel."))
    demoted = DEMOTED.format(stored="rel")
    assert rel_set == (
        "SET rel.slot_id = child_row.slot_id, "
        f"rel.direction = CASE WHEN child_row.direction IS NULL OR {demoted} THEN rel.direction "
        "ELSE child_row.direction END, "
        f"rel.provenance = CASE WHEN (child_row.direction IS NULL) OR {demoted} THEN rel.provenance "
        "ELSE coalesce(child_row.provenance, $provenance_enriched) END, "
        "rel._tenant = $tenant"
    )
    # The edge identity is never governed; the static child node takes no line key.
    assert "rel.slot_id = CASE" not in cypher
    assert "ON CREATE SET c += child_row{.*, slot_id: null, direction: null, provenance: null}" in cypher


def test_child_node_values_follow_the_item_provenance(precedence_on: None) -> None:
    raw = _declared_raw()
    kind = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Kind")
    kind["managedby"] = "sync"
    kind["properties"] += [{"name": "grade", "type": "string"}, {"name": ITEM_PROVENANCE, **ENUM}]
    station = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Station")
    items = next(prop for prop in station["properties"] if prop["name"] == "handled_kinds")["items"]["properties"]
    items.append({"name": "grade", "type": "string"})
    child = raw["sync"]["endpoints"][0]["childsync"][0]
    for key in ("edgeidentity", "edgeproperties"):
        child.pop(key)
    raw["gates"] = [gate for gate in raw["gates"] if gate["type"] != "traversal"]
    spec = DomainSpec(**raw)
    cypher = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/stations"), [])
    demoted = DEMOTED.format(stored="c")
    assert f"grade: CASE WHEN child_row.grade IS NULL OR {demoted} THEN c.grade ELSE child_row.grade END" in cypher
    assert "c.code" not in cypher  # the child id is never governed
    assert f"provenance: CASE WHEN (child_row.grade IS NULL) OR {demoted}" in cypher


def test_match_set_fieldsupdated_resolves_precedence(precedence_on: None) -> None:
    raw = _declared_raw()
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/station-bands",
            "targetnode": "Station",
            "idproperty": "station_id",
            "batchstrategy": "UNWINDMATCHSET",
            "fieldsupdated": ["band", "band_provenance", "station_id"],
        }
    )
    spec = DomainSpec(**raw)
    cypher = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/station-bands"), [])
    keep = KEEP_BAND.replace("node_row.", "row.node.")
    assert "n.station_id = row.node.station_id" in cypher  # not governed: written as declared
    assert f"n.band = CASE WHEN {keep} THEN n.band ELSE row.node.band END" in cypher
    assert "n.band_provenance = row.node.band_provenance" not in cypher
    assert cypher.index("n.band = CASE") < cypher.index("n.band_provenance = CASE")


def test_match_set_without_fieldsupdated_resolves_precedence(precedence_on: None) -> None:
    raw = _declared_raw()
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/station-patch",
            "targetnode": "Station",
            "idproperty": "station_id",
            "batchstrategy": "UNWINDMATCHSET",
        }
    )
    spec = DomainSpec(**raw)
    lines = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/station-patch"), []).splitlines()
    assert lines[2] == "WITH n, row, row.node AS node_row"
    assert f"band: CASE WHEN {KEEP_BAND} THEN n.band ELSE node_row.band END" in lines[3]


def test_precedence_refuses_the_legacy_row_paths(precedence_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_declared_projection_enabled", False)
    spec = _declared()
    with pytest.raises(ValueError, match="requires sync_declared_projection_enabled"):
        SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/stations"), [])


def test_projection_keeps_companions_for_the_write(precedence_on: None) -> None:
    spec = _declared()
    rows, undeclared = SyncGenerator(spec).project_batch(
        _endpoint(spec, "/v1/sync/stations"),
        [
            {
                "station_id": "s1",
                "band": "A",
                "band_provenance": "observed",
                "handled_kinds": [{"code": "k", "slot_id": "1", "direction": "in", "provenance": "enriched"}],
            }
        ],
    )
    assert undeclared == {}
    assert rows[0]["node"] == {"station_id": "s1", "band": "A", "band_provenance": "observed"}
    assert rows[0]["children"]["handled_kinds"] == [
        {"code": "k", "slot_id": "1", "direction": "in", "provenance": "enriched"}
    ]
