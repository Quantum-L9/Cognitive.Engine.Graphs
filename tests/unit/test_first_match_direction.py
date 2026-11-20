"""First PlasticOS match-direction contract (TASK-016)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec
from engine.traversal.assembler import TraversalAssembler
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DIRECTION = "supply_opportunity_to_buyer_facility"
LEGACY = "intake_to_buyer"


def test_executable_spec_declares_first_direction() -> None:
    data = yaml.safe_load((DESIGN_PACKS_ROOT / "plasticos/spec.yaml").read_text())
    directions = data["queryschema"]["matchdirections"]
    assert DIRECTION in directions
    assert directions[0] == DIRECTION
    assert LEGACY not in directions
    text = (DESIGN_PACKS_ROOT / "plasticos/spec.yaml").read_text()
    assert LEGACY not in text


def test_domain_pack_loader_exposes_first_direction() -> None:
    spec = DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT)).load_domain("plasticos")
    assert isinstance(spec, DomainSpec)
    assert DIRECTION in spec.queryschema.matchdirections
    assert spec.queryschema.matchdirections[0] == DIRECTION


def test_traversal_assembler_accepts_first_direction() -> None:
    """The direction assembles; the polymer constraint is a gate, not a traversal MATCH (CEG-COR-013).

    A required `MATCH (candidate:Facility)-[:PROCESSES]->(polymer)` step used to
    be asserted here; it ignored the requested polymer and multiplied rows, so
    the pack now expresses it as an EXISTS traversal gate on $polymer_type.
    """
    from engine.gates.compiler import GateCompiler

    loader = DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT))
    spec = loader.load_domain("plasticos")
    clauses = TraversalAssembler(spec).assemble_traversal(DIRECTION)
    assert clauses == []
    where = GateCompiler(spec).compile_all_gates(DIRECTION)
    assert "MATCH (candidate)-[rel:PROCESSES]->(t:PolymerFamily) WHERE t.code = $polymer_type" in where
    assert "rel.direction = " in where
    assert "rel.form IS NOT NULL AND rel.form <> " in where


def test_legacy_direction_not_in_executable_contract() -> None:
    loader = DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT))
    spec = loader.load_domain("plasticos")
    assert LEGACY not in spec.queryschema.matchdirections
    assembler = TraversalAssembler(spec)
    legacy_clauses = assembler.assemble_traversal(LEGACY)
    canonical_clauses = assembler.assemble_traversal(DIRECTION)
    assert len(legacy_clauses) <= len(canonical_clauses)
