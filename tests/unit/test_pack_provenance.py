"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, provenance, contract, domain-packs]
owner: engine-team
status: active
--- /L9_META ---

Provenance declarations in the shipped packs (#351 revision; naming per EIE
#419). The PlasticOS design pack, its TEST_ONLY mirror and the synthneutral
TEST_ONLY pack declare ``F_provenance: enum[observed|enriched]`` beside every
synced non-identity row field, and one item-level ``provenance`` on each
childsync item and its line edge; with ``sync_provenance_precedence_enabled``
on, every endpoint admits. EIE declares the same on its cartridges, so the two
sides stay identical. Engine behaviour: ``tests/unit/test_sync_provenance.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.sync.generator import SyncGenerator
from engine.sync.row_contract import ITEM_PROVENANCE, PROVENANCE_VALUES, endpoint_shape_defects
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit

TEST_ONLY_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "test_only_packs"
SYNTHNEUTRAL = TEST_ONLY_ROOT / "synthneutral" / "spec.yaml"
PLASTICOS_DESIGN = DESIGN_PACKS_ROOT / "plasticos" / "spec.yaml"
PLASTICOS_TEST_ONLY = TEST_ONLY_ROOT / "plasticos" / "spec.yaml"
DEMOTED = (
    "(coalesce(child_row.provenance, $provenance_enriched) = $provenance_enriched "
    "AND {stored}.provenance = $provenance_observed)"
)


@pytest.fixture
def precedence_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_provenance_precedence_enabled", True)


def _spec(path: Path) -> DomainSpec:
    return DomainSpec(**yaml.safe_load(path.read_text()))


def _endpoint(spec: DomainSpec, path: str) -> Any:
    return next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == path)


@pytest.mark.parametrize("path", [SYNTHNEUTRAL, PLASTICOS_DESIGN, PLASTICOS_TEST_ONLY], ids=lambda p: p.parent.name)
def test_shipped_packs_admit_with_precedence_on(path: Path, precedence_on: None) -> None:
    spec = _spec(path)
    for endpoint in spec.sync.endpoints:
        assert endpoint_shape_defects(spec, endpoint) == [], endpoint.path


def _item_provenance(properties: list[dict[str, Any]]) -> bool:
    assert not any(prop["name"].endswith("_provenance") for prop in properties)
    declared = [prop for prop in properties if prop["name"] == ITEM_PROVENANCE]
    return len(declared) == 1 and declared[0]["type"] == "enum" and set(declared[0]["values"]) == PROVENANCE_VALUES


def _companions(properties: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    names = {prop["name"] for prop in properties}
    governed = set()
    for prop in properties:
        if prop["name"].endswith("_provenance"):
            assert prop["type"] == "enum"
            assert set(prop["values"]) == PROVENANCE_VALUES
            governed.add(prop["name"].removesuffix("_provenance"))
    return names, governed


# Identity keys and graph-written properties carry no provenance.
SYNTHNEUTRAL_EXEMPT = {"station_id", "handled_kinds"}
PLASTICOS_EXEMPT = {
    "Facility": {"facility_id", "entity_ref", "community_id", "processed_polymers"},
    "SupplyOpportunity": {"opportunity_id", "facility_id"},
}


def test_synthneutral_declares_a_companion_for_every_synced_field() -> None:
    raw = yaml.safe_load(SYNTHNEUTRAL.read_text())
    station = next(node for node in raw["ontology"]["nodes"] if node["label"] == "Station")
    names, governed = _companions(station["properties"])
    synced = {name for name in names if not name.endswith("_provenance")} - SYNTHNEUTRAL_EXEMPT
    assert governed == synced
    items = next(prop for prop in station["properties"] if prop["name"] == "handled_kinds")["items"]["properties"]
    assert _item_provenance(items)
    edge = next(edge for edge in raw["ontology"]["edges"] if edge["type"] == "HANDLES")
    assert _item_provenance(edge["properties"])
    child = raw["sync"]["endpoints"][0]["childsync"][0]
    assert {"direction", ITEM_PROVENANCE} <= set(child["edgeproperties"])


@pytest.mark.parametrize("path", [PLASTICOS_DESIGN, PLASTICOS_TEST_ONLY], ids=["design", "test_only"])
def test_plasticos_declares_a_companion_for_every_synced_field(path: Path) -> None:
    raw = yaml.safe_load(path.read_text())
    nodes = {node["label"]: node for node in raw["ontology"]["nodes"]}
    for label, exempt in PLASTICOS_EXEMPT.items():
        names, governed = _companions(nodes[label]["properties"])
        assert governed == {name for name in names if not name.endswith("_provenance")} - exempt, label
    items = next(prop for prop in nodes["Facility"]["properties"] if prop["name"] == "processed_polymers")
    assert _item_provenance(items["items"]["properties"])
    edge = next(edge for edge in raw["ontology"]["edges"] if edge["type"] == "PROCESSES")
    assert _item_provenance(edge["properties"])
    child = _endpoint(DomainSpec(**raw), "/v1/sync/facilities").childsync[0]
    assert {"direction", "form", ITEM_PROVENANCE} <= set(child.edgeproperties or [])
    assert child.fromrow == ["direction", "form"]


def test_plasticos_test_only_pack_carries_the_design_pack_body_verbatim() -> None:
    design = PLASTICOS_DESIGN.read_text().splitlines()[9:]
    test_only = PLASTICOS_TEST_ONLY.read_text().splitlines()
    start = test_only.index(design[0])
    assert test_only[start : start + len(design)] == design


def test_plasticos_line_values_follow_the_item_provenance(precedence_on: None) -> None:
    spec = _spec(PLASTICOS_DESIGN)
    cypher = SyncGenerator(spec).generate_sync_query(_endpoint(spec, "/v1/sync/facilities"), [])
    demoted = DEMOTED.format(stored="rel")
    assert (
        f"rel.provenance = CASE WHEN (child_row.direction IS NULL AND child_row.form IS NULL) OR {demoted} "
        "THEN rel.provenance ELSE coalesce(child_row.provenance, $provenance_enriched) END, rel._tenant = $tenant"
    ) in cypher
    assert "rel.line_id = child_row.line_id" in cypher
