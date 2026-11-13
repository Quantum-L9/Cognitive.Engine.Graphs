"""Unit tests — ScoringAssembler: clause assembly, weight overrides.

Real API (engine/scoring/assembler.py):
    ScoringAssembler(domain_spec, graph_driver=None)
        .assemble_scoring_clause(match_direction: str, weights: dict[str, float],
                                 pareto_candidates=None)
        -> tuple[str, dict | None]   # (Cypher WITH clause, pareto metadata)
"""

from __future__ import annotations

import pytest

from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit


@pytest.fixture
def plasticos_spec():
    """Load plasticos spec, skip if not loadable."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT))
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
    clause, _meta = assembler.assemble_scoring_clause(
        "supply_opportunity_to_buyer_facility", _default_weights(plasticos_spec)
    )
    assert isinstance(clause, str)
    assert len(clause) > 0


def test_scoring_clause_contains_composite_score(plasticos_spec):
    """Scoring clause contains the composite score expression."""
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(plasticos_spec)
    clause, _meta = assembler.assemble_scoring_clause(
        "supply_opportunity_to_buyer_facility", _default_weights(plasticos_spec)
    )
    assert "score" in clause.lower()
    assert "AS" in clause


def test_weight_override_changes_output(plasticos_spec):
    """Overriding a dimension weight changes the generated clause."""
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(plasticos_spec)
    if not plasticos_spec.scoring.dimensions:
        pytest.skip("No scoring dimensions in plasticos spec")

    base_weights = _default_weights(plasticos_spec)
    base_clause, _ = assembler.assemble_scoring_clause("supply_opportunity_to_buyer_facility", base_weights)

    override_weights = dict(base_weights)
    first_key = plasticos_spec.scoring.dimensions[0].weightkey
    override_weights[first_key] = 0.9999
    override_clause, _ = assembler.assemble_scoring_clause("supply_opportunity_to_buyer_facility", override_weights)

    assert isinstance(override_clause, str)
    assert override_clause != base_clause


# ── RU-017 / CEG-COR-006: pricealignment float division (RG-021) ─────────────


def _price_dim():
    from engine.config.schema import ComputationType, ScoringDimensionSpec, ScoringSource

    return ScoringDimensionSpec(
        name="price_alignment",
        source=ScoringSource.CANDIDATEPROPERTY,
        computation=ComputationType.PRICEALIGNMENT,
        candidateprop="rate_per_ton",
        queryprop="target_price",
        weightkey="w_price",
        defaultweight=0.15,
    )


def test_pricealignment_casts_both_operands_to_float(plasticos_spec):
    """INTEGER/INTEGER truncates in Cypher (450/500 -> 0); both operands must be toFloat()."""
    from engine.scoring.assembler import ScoringAssembler

    expr = ScoringAssembler(plasticos_spec)._compile_pricealignment(_price_dim())
    assert "toFloat(candidate.rate_per_ton) / toFloat($target_price)" in expr
    assert "candidate.rate_per_ton / $target_price" not in expr
    # Preserved: NULL / <= 0 guards still return defaultwhennull, $q stays a parameter.
    assert "WHEN $target_price IS NULL OR $target_price <= 0 THEN 0.0" in expr
    assert "WHEN candidate.rate_per_ton IS NULL OR candidate.rate_per_ton <= 0 THEN 0.0" in expr


def test_pricealignment_flag_off_restores_legacy_expression(plasticos_spec, monkeypatch):
    """C-21: pricealignment_float_division=False emits the legacy text verbatim."""
    from engine.config.settings import settings
    from engine.scoring.assembler import ScoringAssembler

    monkeypatch.setattr(settings, "pricealignment_float_division", False)
    expr = ScoringAssembler(plasticos_spec)._compile_pricealignment(_price_dim())
    assert expr == (
        "CASE "
        "  WHEN $target_price IS NULL OR $target_price <= 0 THEN 0.0 "
        "  WHEN candidate.rate_per_ton IS NULL OR candidate.rate_per_ton <= 0 THEN 0.0 "
        "  ELSE toFloat(1.0 - abs(log(candidate.rate_per_ton / $target_price)) / 2.0) "
        "END"
    )


def test_plasticos_price_dimensions_compile_float_division_both_directions(plasticos_spec):
    """Both plasticos pricealignment dimensions (one per direction) compile the float form."""
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(plasticos_spec)
    weights = _default_weights(plasticos_spec)
    for direction in ("supply_opportunity_to_buyer_facility", "buyer_demand_to_supply_opportunity"):
        clause, _ = assembler.assemble_scoring_clause(direction, weights)
        assert "toFloat(candidate.rate_per_ton) / toFloat($target_price)" in clause, direction
