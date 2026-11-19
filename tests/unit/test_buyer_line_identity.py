"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, match, buyer]
owner: engine-team
status: active
--- /L9_META ---

G02-BUYER-GRAPH. A buyer is a stored In capability line with a form.

The line is the PROCESSES relationship, keyed by line_id. PolymerFamily stays
the taxonomy, one node per code. direction and form on the facility row fill a
line that omits them. quantity_per_load_lbs is not a graph property.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.config.loader import DomainPackLoader
from engine.gates.compiler import GateCompiler
from engine.sync.generator import SyncGenerator
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit

DIRECTION = "supply_opportunity_to_buyer_facility"
POLYMER = "HDPE"


def _spec():
    return DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT)).load_domain("plasticos")


def _facilities(spec):
    return next(endpoint for endpoint in spec.sync.endpoints if endpoint.path == "/v1/sync/facilities")


def _lines(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = []
    for row in rows:
        facility_id = row["node"].get("facility_id")
        assert "direction" not in row["node"]
        assert "form" not in row["node"]
        assert "line_id" not in row["node"]
        assert "quantity_per_load_lbs" not in row["node"]
        for child in row["children"]["processed_polymers"]:
            found.append(
                (facility_id, child.get("line_id"), child.get("direction"), child.get("form"), child.get("code"))
            )
    return found


def _is_buyer_line(line: tuple[Any, ...], polymer: str = POLYMER) -> bool:
    """The same conjunction the traversal gate compiles onto one relationship."""
    _facility, line_id, direction, form, code = line
    return (
        code == polymer
        and direction == "in"
        and isinstance(line_id, str)
        and line_id != ""
        and isinstance(form, str)
        and form != ""
    )


SHAPES: dict[str, list[dict[str, Any]]] = {
    "out_only_supplier": [
        {
            "facility_id": "site-out",
            "name": "Out only supplier",
            "direction": "out",
            "form": "REGRIND",
            "processed_polymers": [{"code": "HDPE", "line_id": "out-1"}],
        }
    ],
    "in_only_buyer": [
        {
            "facility_id": "site-in",
            "name": "In only buyer",
            "direction": "in",
            "form": "PALLETS",
            "processed_polymers": [{"code": "HDPE", "line_id": "in-1"}],
            "contamination_tolerance": 5.0,
            "capacity_tons_month": 40.0,
            "entity_ref": "res.partner:102",
        }
    ],
    "two_lines_same_polymer": [
        {
            "facility_id": "site-lines",
            "name": "Two shred lines",
            "processed_polymers": [
                {"code": "HDPE", "line_id": "shred-1", "form": "PALLETS", "direction": "in"},
                {"code": "HDPE", "line_id": "shred-2", "form": "TOTERS", "direction": "in"},
            ],
        }
    ],
    "dual_role_facility": [
        {
            "facility_id": "site-dual",
            "name": "Dual role",
            "direction": "in",
            "form": "PALLETS",
            "processed_polymers": [{"code": "HDPE", "line_id": "in-line"}],
        },
        {
            "facility_id": "site-dual",
            "name": "Dual role",
            "direction": "out",
            "form": "REGRIND",
            "processed_polymers": [{"code": "HDPE", "line_id": "out-line"}],
        },
    ],
    "missing_direction_and_form": [
        {
            "facility_id": "site-blank",
            "name": "Blank direction and form",
            "direction": "",
            "form": "",
            "processed_polymers": [{"code": "HDPE"}],
        }
    ],
}


def test_five_buyer_lines_stay_distinct_and_only_stored_in_is_eligible() -> None:
    spec = _spec()
    endpoint = _facilities(spec)
    generator = SyncGenerator(spec)
    kept: dict[str, list[tuple[Any, ...]]] = {}
    for name, batch in SHAPES.items():
        rows, dropped = generator.project_batch(endpoint, batch)
        assert "direction" not in dropped
        assert "form" not in dropped
        assert "line_id" not in dropped
        assert "quantity_per_load_lbs" not in dropped
        kept[name] = _lines(rows)
        if name == "in_only_buyer":
            assert rows[0]["node"]["entity_ref"] == "res.partner:102"

    assert kept["out_only_supplier"] == [("site-out", "out-1", "out", "REGRIND", "HDPE")]
    assert [_is_buyer_line(line) for line in kept["out_only_supplier"]] == [False]

    assert kept["in_only_buyer"] == [("site-in", "in-1", "in", "PALLETS", "HDPE")]
    assert [_is_buyer_line(line) for line in kept["in_only_buyer"]] == [True]

    assert kept["two_lines_same_polymer"] == [
        ("site-lines", "shred-1", "in", "PALLETS", "HDPE"),
        ("site-lines", "shred-2", "in", "TOTERS", "HDPE"),
    ]
    assert len({line[1] for line in kept["two_lines_same_polymer"]}) == 2

    assert kept["dual_role_facility"] == [
        ("site-dual", "in-line", "in", "PALLETS", "HDPE"),
        ("site-dual", "out-line", "out", "REGRIND", "HDPE"),
    ]
    assert [_is_buyer_line(line) for line in kept["dual_role_facility"]] == [True, False]

    assert kept["missing_direction_and_form"] == [("site-blank", None, "", "", "HDPE")]
    assert [_is_buyer_line(line) for line in kept["missing_direction_and_form"]] == [False]

    cypher = generator.generate_sync_query(endpoint, [])
    assert "MERGE (n:Facility {facility_id: row.node.facility_id})" in cypher
    assert "MERGE (c:PolymerFamily {code: child_row.code})" in cypher
    assert "MERGE (n)-[rel:PROCESSES {line_id: child_row.line_id}]->(c)" in cypher
    assert "quantity_per_load_lbs" not in cypher
    node_names = {prop.name for node in spec.ontology.nodes for prop in node.properties}
    query_names = {field.name for field in spec.queryschema.fields}
    assert {"capacity_tons_month", "rate_per_ton"} <= node_names
    assert "volume_tons" in query_names
    assert "quantity_per_load_lbs" not in node_names
    assert "quantity_per_load_lbs" not in query_names

    compiler = GateCompiler(spec)
    where = compiler.compile_all_gates(DIRECTION)
    start = where.index("EXISTS { MATCH (candidate)-[rel:PROCESSES]->(t:PolymerFamily)")
    exists = where[start : where.index("}", start)]
    assert "t.code = $polymer_type" in exists
    assert "rel.direction = $gate_polymer_capability_gate_edge0" in exists
    assert "rel.form IS NOT NULL AND rel.form <> $gate_polymer_capability_gate_edge1blank" in exists
    assert compiler.last_query_params["gate_polymer_capability_gate_edge0"] == "in"
    assert compiler.last_query_params["gate_polymer_capability_gate_edge1blank"] == ""
    assert "capacity_tons_month" not in where
    assert "$volume_tons" not in where
    assert "quantity_per_load_lbs" not in where


def test_quantity_per_load_stays_on_the_query_and_off_the_graph() -> None:
    spec = _spec()
    endpoint = _facilities(spec)
    batch = [
        {
            "facility_id": "site-in",
            "name": "In only buyer",
            "direction": "in",
            "form": "PALLETS",
            "quantity_per_load_lbs": 40000,
            "processed_polymers": [{"code": "HDPE", "line_id": "in-1"}],
        }
    ]
    rows, dropped = SyncGenerator(spec).project_batch(endpoint, batch)
    assert dropped["quantity_per_load_lbs"] == 1
    assert "quantity_per_load_lbs" not in rows[0]["node"]
    assert "quantity_per_load_lbs" not in rows[0]["children"]["processed_polymers"][0]

    from engine.handlers import handle_match, init_dependencies
    from engine.state import get_state

    calls: list[dict[str, Any]] = []

    class Driver:
        async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
            calls.append({"cypher": cypher, "parameters": parameters, "database": database})
            return []

    get_state().reset()
    init_dependencies(Driver(), DomainPackLoader(str(DESIGN_PACKS_ROOT)))
    try:
        asyncio.run(
            handle_match(
                "plasticos",
                {
                    "query": {
                        "polymer_type": "HDPE",
                        "form": "PALLETS",
                        "quantity_per_load_lbs": 40000,
                        "contamination_pct": 2.5,
                        "volume_tons": 18.0,
                    },
                    "match_direction": DIRECTION,
                    "top_n": 5,
                },
            )
        )
    finally:
        get_state().reset()

    match_calls = [call for call in calls if "LIMIT $top_n" in call["cypher"]]
    assert match_calls
    cypher = match_calls[0]["cypher"]
    assert "quantity_per_load_lbs" not in cypher
    assert match_calls[0]["parameters"]["quantity_per_load_lbs"] == 40000
    assert match_calls[0]["parameters"]["volume_tons"] == 18.0
    assert "rel.direction = " in cypher
    assert "capacity_tons_month" not in cypher


def test_line_identity_off_restores_the_unkeyed_polymer_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "buyer_line_identity_enabled", False)
    spec = _spec()
    endpoint = _facilities(spec)
    generator = SyncGenerator(spec)
    rows, dropped = generator.project_batch(endpoint, SHAPES["out_only_supplier"])
    assert dropped["direction"] == 1
    assert dropped["form"] == 1
    assert rows[0]["children"]["processed_polymers"] == [{"code": "HDPE"}]
    cypher = generator.generate_sync_query(endpoint, [])
    assert "MERGE (n)-[:PROCESSES]->(c)" in cypher
    assert "line_id: child_row.line_id" not in cypher
    where = GateCompiler(spec).compile_all_gates(DIRECTION)
    assert "[rel:PROCESSES]" not in where
    assert "rel.direction" not in where


def test_undeclared_edge_predicate_property_is_rejected() -> None:
    from pydantic import ValidationError

    from engine.config.schema import EdgePredicateSpec

    spec = _spec()
    dumped = spec.model_dump()
    for gate in dumped["gates"]:
        if gate["name"] == "polymer_capability_gate":
            gate["edgepredicates"].append({"property": "color_code", "equals": "NATURAL"})
    with pytest.raises(ValidationError, match="not declared on edge"):
        type(spec).model_validate(dumped)

    gate = next(item for item in spec.gates if item.name == "polymer_capability_gate")
    gate.edgepredicates = [
        *(gate.edgepredicates or []),
        EdgePredicateSpec(property="color_code", equals="NATURAL"),
    ]
    with pytest.raises(ValueError, match="not declared on edge"):
        GateCompiler(spec).compile_all_gates(DIRECTION)
