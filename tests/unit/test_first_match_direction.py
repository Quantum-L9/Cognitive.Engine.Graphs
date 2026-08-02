"""First PlasticOS match-direction contract (TASK-016)."""

from __future__ import annotations

from pathlib import Path

import yaml

from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec
from engine.traversal.assembler import TraversalAssembler

ROOT = Path(__file__).resolve().parents[2]
DIRECTION = "supply_opportunity_to_buyer_facility"
LEGACY = "intake_to_buyer"


def test_executable_spec_declares_first_direction_only() -> None:
    data = yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_text())
    directions = data["queryschema"]["matchdirections"]
    assert directions == [DIRECTION]
    assert LEGACY not in directions
    text = (ROOT / "domains/plasticos/spec.yaml").read_text()
    assert LEGACY not in text


def test_domain_pack_loader_exposes_first_direction() -> None:
    spec = DomainPackLoader(config_path=str(ROOT / "domains")).load_domain("plasticos")
    assert isinstance(spec, DomainSpec)
    assert spec.queryschema.matchdirections == [DIRECTION]


def test_traversal_assembler_accepts_first_direction() -> None:
    loader = DomainPackLoader(config_path=str(ROOT / "domains"))
    assembler = TraversalAssembler(loader.load_domain("plasticos"))
    clauses = assembler.assemble_traversal(DIRECTION)
    assert isinstance(clauses, list)
    assert len(clauses) > 0


def test_legacy_direction_not_in_executable_contract() -> None:
    loader = DomainPackLoader(config_path=str(ROOT / "domains"))
    spec = loader.load_domain("plasticos")
    assert LEGACY not in spec.queryschema.matchdirections
    assembler = TraversalAssembler(spec)
    legacy_clauses = assembler.assemble_traversal(LEGACY)
    canonical_clauses = assembler.assemble_traversal(DIRECTION)
    assert len(legacy_clauses) <= len(canonical_clauses)
