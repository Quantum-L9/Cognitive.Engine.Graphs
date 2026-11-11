"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, schema, admission, contract, domain-neutrality]
owner: engine-team
status: active
--- /L9_META ---

The domain spec declares ``list`` and ``map`` property shapes, and admission
refuses a sync contract the spec cannot state:

* schema — ``items`` iff list, ``properties`` iff map, no list of lists;
* admission — a stored ``map`` / ``list<map>``, an undeclared childsync field,
  a childsync ``targetid`` outside the item map, a taxonomy field of the wrong
  shape, an undeclared ``fieldsupdated`` entry.

Proven on the synthetic ``orbital-logistics`` pack (vocabulary no shipped pack
shares); every shipped pack still validates.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from engine.config.loader import admission_defects, withholding_reason
from engine.config.schema import DomainSpec, PropertySpec, QueryFieldSpec
from engine.config.settings import settings
from engine.sync.row_contract import endpoint_shape_defects, stored_shape_defects
from tests.fixtures import typed_sync as fx
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

# ── Schema: list / map declarations ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"name": "tags", "type": "list"}, "type list requires items"),
        ({"name": "where", "type": "map"}, "type map requires a non-empty properties list"),
        ({"name": "where", "type": "map", "properties": []}, "type map requires a non-empty properties list"),
        (
            {"name": "grid", "type": "list", "items": {"type": "list", "items": {"type": "string"}}},
            "a list of lists is not a declarable shape",
        ),
        ({"name": "code", "type": "string", "items": {"type": "string"}}, "only for type list or map, not string"),
        (
            {"name": "tags", "type": "list", "items": {"type": "string"}, "properties": [{"name": "a", "type": "int"}]},
            "declares its element shape in items, not properties",
        ),
        (
            {"name": "rows", "type": "list", "items": {"type": "map"}},
            "items: type map requires a non-empty properties list",
        ),
        (
            {
                "name": "where",
                "type": "map",
                "properties": [{"name": "lat", "type": "float"}, {"name": "lat", "type": "float"}],
            },
            "duplicate map property name(s) ['lat']",
        ),
    ],
)
def test_shape_declarations_are_validated(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(PydanticValidationError, match=re.escape(message)):
        PropertySpec.model_validate(raw)


def test_the_shared_syntax_parses_and_renders() -> None:
    certifications = PropertySpec.model_validate(
        {"name": "certifications", "type": "list", "items": {"type": "string"}}
    )
    polymers = PropertySpec.model_validate(
        {
            "name": "processed_polymers",
            "type": "list",
            "items": {"type": "map", "properties": [{"name": "code", "type": "string", "required": True}]},
        }
    )
    location = PropertySpec.model_validate(
        {"name": "location", "type": "map", "properties": [{"name": "lat", "type": "float"}]}
    )
    assert (certifications.declared_type, certifications.wire_only) == ("list<string>", False)
    assert (polymers.declared_type, polymers.wire_only) == ("list<map>", True)
    assert (location.declared_type, location.wire_only) == ("map", True)


def test_list_and_map_are_not_query_field_types() -> None:
    with pytest.raises(PydanticValidationError, match="query fields are scalars"):
        QueryFieldSpec.model_validate({"name": "codes", "type": "list"})


def test_every_shipped_spec_still_validates() -> None:
    paths = sorted([*fx.REPO_ROOT.glob("domains/*/spec.yaml"), *SYNTHETIC_DOMAINS_ROOT.glob("*/spec.yaml")])
    paths.append(fx.REPO_ROOT / "domains" / "_template.yaml")
    for path in paths:
        spec = DomainSpec.model_validate(yaml.safe_load(path.read_text()))
        assert stored_shape_defects(spec) == [], path
        for endpoint in spec.sync.endpoints if spec.sync else []:
            assert endpoint_shape_defects(spec, endpoint) == [], (path, endpoint.path)


# ── Admission ─────────────────────────────────────────────────────────────────


def test_a_stored_map_no_childsync_consumes_is_an_admission_defect() -> None:
    raw = fx.orbital_raw()
    fx.node_props(raw, "OrbitalDepot").append(
        {"name": "location", "type": "map", "properties": [{"name": "lat", "type": "float"}]}
    )
    spec = fx.spec(raw)
    [defect] = stored_shape_defects(spec)
    assert defect.startswith("ontology node OrbitalDepot property 'location': declared map, a wire-only shape")
    assert defect in admission_defects(spec)
    reason = withholding_reason(spec)
    assert reason is not None
    assert "property 'location'" in reason


def test_an_unconsumed_list_of_maps_and_an_edge_map_are_admission_defects() -> None:
    raw = fx.orbital_raw()
    fx.depot_endpoint(raw)["childsync"] = []
    edge = next(edge for edge in raw["ontology"]["edges"] if edge["type"] == "RESUPPLIED_BY")
    edge["properties"].append(
        {
            "name": "manifest",
            "type": "list",
            "items": {"type": "map", "properties": [{"name": "sku", "type": "string"}]},
        }
    )
    defects = stored_shape_defects(fx.spec(raw))
    assert any("property 'stocked_grades': declared list<map>" in defect for defect in defects)
    assert any(defect.startswith("ontology edge RESUPPLIED_BY property 'manifest'") for defect in defects)


@pytest.mark.parametrize(
    ("mutate", "defect"),
    [
        (
            lambda raw: fx.drop_depot_prop(raw, "stocked_grades"),
            "childsync field 'stocked_grades' must be declared on OrbitalDepot as list<map>, found undeclared",
        ),
        (
            lambda raw: fx.depot_props(raw)["stocked_grades"].update({"items": {"type": "string"}}),
            "childsync field 'stocked_grades' must be declared on OrbitalDepot as list<map>, found list<string>",
        ),
        (
            lambda raw: fx.depot_props(raw)["stocked_grades"]["items"]["properties"][0].update({"name": "code"}),
            "childsync targetid 'grade_code' must be a required scalar property of stocked_grades's items",
        ),
        (
            lambda raw: fx.depot_props(raw)["stocked_grades"]["items"]["properties"][0].update({"required": False}),
            "childsync targetid 'grade_code' must be a required scalar property of stocked_grades's items",
        ),
        (
            lambda raw: fx.drop_depot_prop(raw, "served_orbits"),
            (
                "taxonomy field 'served_orbits' must be declared on OrbitalDepot as a scalar or list<scalar>, "
                "found undeclared"
            ),
        ),
        (
            lambda raw: fx.depot_endpoint(raw).update({"fieldsupdated": ["colour"]}),
            "fieldsupdated 'colour' must be a storable property declared on OrbitalDepot, found undeclared",
        ),
    ],
)
def test_endpoint_shape_defects_are_admission_defects(mutate: Any, defect: str) -> None:
    raw = fx.orbital_raw()
    mutate(raw)
    spec = fx.spec(raw)
    expected = f"sync {fx.DEPOTS}: {defect}"
    assert expected in endpoint_shape_defects(spec, fx.endpoint(spec, fx.DEPOTS))
    assert expected in admission_defects(spec)


def test_a_list_of_maps_taxonomy_field_is_refused() -> None:
    raw = fx.orbital_raw()
    fx.depot_props(raw)["served_orbits"].update(
        {"items": {"type": "map", "properties": [{"name": "class_code", "type": "string"}]}}
    )
    spec = fx.spec(raw)
    assert (
        f"sync {fx.DEPOTS}: taxonomy field 'served_orbits' must be declared on OrbitalDepot as a scalar or "
        "list<scalar>, found list<map>"
    ) in admission_defects(spec)


def test_a_withheld_pack_is_served_only_under_the_operator_override(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = fx.orbital_raw()
    fx.drop_depot_prop(raw, "served_orbits")
    spec = fx.spec(raw)
    reason = withholding_reason(spec)
    assert reason is not None
    assert "taxonomy field 'served_orbits'" in reason
    monkeypatch.setattr(settings, "unvalidated_domain_packs_enabled", True)
    assert withholding_reason(spec) is None
