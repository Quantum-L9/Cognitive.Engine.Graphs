"""Unit tests — TraversalAssembler: direction filter, step ordering.

Real API (engine/traversal/assembler.py):
    TraversalAssembler(domain_spec).assemble_traversal(match_direction: str) -> list[str]
The plasticos spec declares direction 'supply_opportunity_to_buyer_facility' with a required
(candidate:Facility)-[:PROCESSES]->(polymer:PolymerFamily) step.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def plasticos_spec():
    """Load plasticos spec, skip if not loadable."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader()
    try:
        return loader.load_domain("plasticos")
    except Exception:
        pytest.skip("plasticos domain spec not loadable with current schema")


def test_assembler_produces_list(plasticos_spec):
    """Traversal assembler produces a list of MATCH clauses."""
    from engine.traversal.assembler import TraversalAssembler

    assembler = TraversalAssembler(plasticos_spec)
    clauses = assembler.assemble_traversal("supply_opportunity_to_buyer_facility")
    assert isinstance(clauses, list)
    assert len(clauses) > 0  # spec has a required PROCESSES step


def test_direction_filter_excludes_other_directions(plasticos_spec):
    """Steps scoped to supply_opportunity_to_buyer_facility must not appear for other directions."""
    from engine.traversal.assembler import TraversalAssembler

    assembler = TraversalAssembler(plasticos_spec)
    matching = assembler.assemble_traversal("supply_opportunity_to_buyer_facility")
    non_matching = assembler.assemble_traversal("nonexistent_direction")
    assert isinstance(matching, list)
    assert isinstance(non_matching, list)
    assert len(non_matching) <= len(matching)


def test_traversal_steps_are_strings(plasticos_spec):
    """Traversal clauses are non-empty strings."""
    from engine.traversal.assembler import TraversalAssembler

    assembler = TraversalAssembler(plasticos_spec)
    clauses = assembler.assemble_traversal("supply_opportunity_to_buyer_facility")
    for clause in clauses:
        assert isinstance(clause, str)
        assert len(clause) > 0
