"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [matching, scoring]
status: active
--- /L9_META ---

Unit tests — ScoringAssembler: clause assembly, weight overrides.

Real API (engine/scoring/assembler.py):
    ScoringAssembler(domain_spec, graph_driver=None)
        .assemble_scoring_clause(match_direction: str, weights: dict[str, float],
                                 pareto_candidates=None)
        -> tuple[str, dict | None]   # (Cypher WITH clause, pareto metadata)
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


def _default_weights(spec) -> dict[str, float]:
    """Build the default weight map from the spec's scoring dimensions."""
    return {dim.weightkey: dim.defaultweight for dim in spec.scoring.dimensions}


def test_assembler_loads_from_plasticos_spec(plasticos_spec):
    """Assembler constructs from the plasticos spec and produces a clause."""
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(plasticos_spec)
    clause, _meta = assembler.assemble_scoring_clause("intake_to_buyer", _default_weights(plasticos_spec))
    assert isinstance(clause, str)
    assert len(clause) > 0


def test_scoring_clause_contains_composite_score(plasticos_spec):
    """Scoring clause contains the composite score expression."""
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(plasticos_spec)
    clause, _meta = assembler.assemble_scoring_clause("intake_to_buyer", _default_weights(plasticos_spec))
    assert "score" in clause.lower()
    assert "AS" in clause


def test_weight_override_changes_output(plasticos_spec):
    """Overriding a dimension weight changes the generated clause."""
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(plasticos_spec)
    if not plasticos_spec.scoring.dimensions:
        pytest.skip("No scoring dimensions in plasticos spec")

    base_weights = _default_weights(plasticos_spec)
    base_clause, _ = assembler.assemble_scoring_clause("intake_to_buyer", base_weights)

    override_weights = dict(base_weights)
    first_key = plasticos_spec.scoring.dimensions[0].weightkey
    override_weights[first_key] = 0.9999
    override_clause, _ = assembler.assemble_scoring_clause("intake_to_buyer", override_weights)

    assert isinstance(override_clause, str)
    assert override_clause != base_clause
