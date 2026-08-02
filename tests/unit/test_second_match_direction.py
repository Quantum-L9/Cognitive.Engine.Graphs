"""Second PlasticOS match-direction contract (TASK-017)."""

from __future__ import annotations

from pathlib import Path

import yaml

from engine.config.loader import DomainPackLoader
from engine.traversal.assembler import TraversalAssembler

ROOT = Path(__file__).resolve().parents[2]
DIR1 = "supply_opportunity_to_buyer_facility"
DIR2 = "buyer_demand_to_supply_opportunity"


def _spec_data() -> dict:
    return yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_text())


def test_bidirectional_directions_declared() -> None:
    directions = _spec_data()["queryschema"]["matchdirections"]
    assert directions == [DIR1, DIR2]


def test_second_direction_uses_supply_opportunity_not_facility_capability() -> None:
    data = _spec_data()
    candidates = {(c["label"], c["matchdirection"]) for c in data["matchentities"]["candidate"]}
    assert ("SupplyOpportunity", DIR2) in candidates
    assert ("Facility", DIR2) not in candidates
    assert ("Facility", DIR1) in candidates


def test_active_supply_gates_do_not_use_facility_capacity_property() -> None:
    data = _spec_data()
    reverse_gates = [g for g in data["gates"] if DIR2 in (g.get("matchdirections") or [])]
    assert reverse_gates
    props = {g.get("candidateprop") for g in reverse_gates}
    assert "capacity_tons_month" not in props
    assert "available_tons" in props


def test_loader_and_traversal_for_second_direction() -> None:
    spec = DomainPackLoader(config_path=str(ROOT / "domains")).load_domain("plasticos")
    assert DIR2 in spec.queryschema.matchdirections
    clauses = TraversalAssembler(spec).assemble_traversal(DIR2)
    assert isinstance(clauses, list)
    assert len(clauses) > 0
