"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [health, tests]
tags: [health, tests, readiness, enrichment]
owner: engine-team
status: active
--- /L9_META ---

Test suite for HEALTH service v4.0.
Tests readiness_scorer, gap_prioritizer, enrichment impact measurement, field_analyzer.
The enrichment priority and trigger moved to EIE (operator decision 2026-11-15).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.health.enrichment_trigger import measure_health_impact
from engine.health.field_health import (
    EnrichmentTarget,
    EntityHealth,
    FieldHealth,
    MatchQualityDelta,
    ReadinessScore,
)
from engine.health.gap_prioritizer import (
    estimate_research_cost,
    is_field_populated,
    prioritize_gaps_v2,
)
from engine.health.readiness_scorer import (
    compute_readiness_score_v2,
    compute_staleness_penalty,
    get_grade,
)
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FIXTURES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_domain_spec(
    gates: list[str] | None = None,
    scoring_dims: list[tuple[str, float]] | None = None,
) -> MagicMock:
    """Build a mock DomainSpec with gate and scoring config."""
    spec = MagicMock()
    spec.domain.id = "test_domain"
    spec.domain.description = "test marketplace"

    # Gates
    mock_gates = []
    for name in gates or []:
        g = MagicMock()
        g.candidateprop = name
        mock_gates.append(g)
    spec.gates = mock_gates

    # Scoring
    mock_dims = []
    for name, weight in scoring_dims or []:
        d = MagicMock()
        d.candidateprop = name
        d.source = name
        d.weight = weight
        mock_dims.append(d)
    spec.scoring.dimensions = mock_dims

    # Plugins (no inference rules by default)
    spec.plugins = None

    return spec


def _make_field_health(
    name: str,
    *,
    populated: bool = True,
    confidence: float | None = 0.9,
    gate_critical: bool = False,
    scoring_weight: float = 0.0,
    staleness_days: int | None = None,
) -> FieldHealth:
    return FieldHealth(
        field_name=name,
        is_populated=populated,
        confidence=confidence,
        staleness_days=staleness_days,
        is_gate_critical=gate_critical,
        scoring_weight=scoring_weight,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  READINESS SCORER V2
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReadinessScorer:
    """Tests for readiness_scorer.compute_readiness_score_v2."""

    def test_all_gates_populated(self):
        """All gate-critical fields populated with high confidence → high score."""
        fields = [
            _make_field_health("f1", populated=True, confidence=0.9, gate_critical=True, scoring_weight=0.3),
            _make_field_health("f2", populated=True, confidence=0.8, gate_critical=True, scoring_weight=0.2),
            _make_field_health("f3", populated=True, confidence=0.95, scoring_weight=0.5),
        ]
        spec = _make_domain_spec()
        result = compute_readiness_score_v2(fields, spec)

        assert result.gate_completeness == 1.0
        assert result.overall_score >= 80
        assert result.grade in ("A", "B")
        assert result.blocking_reason is None

    def test_no_gates_defined(self):
        """No gate-critical fields → gate_score defaults to 1.0."""
        fields = [
            _make_field_health("f1", populated=True, scoring_weight=0.5),
            _make_field_health("f2", populated=False, scoring_weight=0.3),
        ]
        spec = _make_domain_spec()
        result = compute_readiness_score_v2(fields, spec)

        assert result.gate_completeness == 1.0
        assert result.overall_score > 0

    def test_below_50_gate_completeness_gives_f(self):
        """< 50% gate completeness → F grade, blocking reason set."""
        fields = [
            _make_field_health("g1", populated=False, gate_critical=True),
            _make_field_health("g2", populated=False, gate_critical=True),
            _make_field_health("g3", populated=True, confidence=0.9, gate_critical=True),
        ]
        spec = _make_domain_spec()
        result = compute_readiness_score_v2(fields, spec)

        # 1/3 populated = 0.33 < 0.5 threshold
        assert result.grade == "F"
        assert result.blocking_reason == "gate_critical_fields_missing"
        assert result.recommended_action == "enrich_gates_first"
        assert "g1" in result.blocking_fields
        assert "g2" in result.blocking_fields

    def test_mixed_gates_above_threshold(self):
        """> 50% gate completeness → not blocked, score computed."""
        fields = [
            _make_field_health("g1", populated=True, confidence=0.9, gate_critical=True),
            _make_field_health("g2", populated=True, confidence=0.8, gate_critical=True),
            _make_field_health("g3", populated=False, gate_critical=True),
            _make_field_health("s1", populated=True, confidence=0.9, scoring_weight=0.5),
        ]
        spec = _make_domain_spec()
        result = compute_readiness_score_v2(fields, spec)

        assert result.gate_completeness == pytest.approx(2 / 3, abs=0.01)
        assert result.grade != "F"  # Not blocked
        assert result.blocking_reason is None

    def test_low_confidence_gate_not_counted(self):
        """Gate field with confidence < 0.70 not counted as populated."""
        fields = [
            _make_field_health("g1", populated=True, confidence=0.5, gate_critical=True),
            _make_field_health("g2", populated=True, confidence=0.9, gate_critical=True),
        ]
        spec = _make_domain_spec()
        result = compute_readiness_score_v2(fields, spec)

        # Only g2 counts → 50% gate completeness → exactly at threshold
        assert result.gate_completeness == 0.5

    # -- RU-108 / CEG-COR-044: an absent confidence is "no signal", not "unreliable" --

    def test_absent_confidence_counts_populated_gate_fields(self):
        """confidence=None on populated fields → counted; readiness is not 0.0/F."""
        fields = [
            _make_field_health("g1", populated=True, confidence=None, gate_critical=True),
            _make_field_health("g2", populated=True, confidence=None, gate_critical=True),
            _make_field_health("s1", populated=True, confidence=None, scoring_weight=0.5),
        ]
        result = compute_readiness_score_v2(fields, _make_domain_spec())

        assert result.gate_completeness == 1.0
        assert result.scoring_dimension_coverage == 1.0
        assert result.blocking_reason is None
        assert result.blocking_fields == []
        assert result.overall_score > 0.0
        assert result.grade != "F"

    def test_explicit_low_confidence_still_discounts_with_absent_elsewhere(self):
        """An explicit 0.5 lowers gate_completeness even when other fields carry no confidence."""
        fields = [
            _make_field_health("g1", populated=True, confidence=0.5, gate_critical=True),
            _make_field_health("g2", populated=True, confidence=None, gate_critical=True),
        ]
        result = compute_readiness_score_v2(fields, _make_domain_spec())

        assert result.gate_completeness == 0.5
        assert result.blocking_fields == ["g1"]

    def test_blocked_result_lists_every_uncounted_gate_field(self):
        """Whenever blocking_reason is set, blocking_fields names the uncounted gate fields."""
        fields = [
            _make_field_health("g1", populated=True, confidence=0.5, gate_critical=True),
            _make_field_health("g2", populated=True, confidence=0.3, gate_critical=True),
            _make_field_health("g3", populated=False, confidence=None, gate_critical=True),
        ]
        result = compute_readiness_score_v2(fields, _make_domain_spec())

        assert result.blocking_reason == "gate_critical_fields_missing"
        assert sorted(result.blocking_fields) == ["g1", "g2", "g3"]

    def test_absent_confidence_flag_off_restores_legacy(self, monkeypatch):
        """health_absent_confidence_counts_enabled=False → None coerces to 0 (legacy outputs)."""
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "health_absent_confidence_counts_enabled", False)
        fields = [
            _make_field_health("g1", populated=True, confidence=None, gate_critical=True),
            _make_field_health("g2", populated=True, confidence=None, gate_critical=True),
        ]
        result = compute_readiness_score_v2(fields, _make_domain_spec())

        assert result.overall_score == 0.0
        assert result.grade == "F"
        assert result.gate_completeness == 0.0
        assert result.blocking_reason == "gate_critical_fields_missing"
        assert result.blocking_fields == []

    # -- RU-110 / CEG-COR-046: the blocked score sits on the same 60% gate scale --

    @staticmethod
    def _score_at(gate_completeness: float) -> float:
        """Readiness with 100 gate fields, ``round(gc*100)`` of them counted; all else fixed.

        One unpopulated scoring field keeps scoring coverage at 0, so the unblocked
        score at gc=0.50 is 40.0 — below the legacy blocked 49.0 at gc=0.49.
        """
        populated = round(gate_completeness * 100)
        fields = [
            _make_field_health(f"g{i}", populated=i < populated, confidence=0.9, gate_critical=True) for i in range(100)
        ]
        fields.append(_make_field_health("s1", populated=False, scoring_weight=0.5))
        return compute_readiness_score_v2(fields, _make_domain_spec()).overall_score

    def test_readiness_is_monotonic_across_the_block_threshold(self):
        scores = [self._score_at(gc) for gc in (0.40, 0.49, 0.50, 0.60)]
        assert scores == sorted(scores), scores
        assert self._score_at(0.49) <= self._score_at(0.50)

    def test_blocked_score_is_the_60_percent_gate_component(self):
        fields = [
            _make_field_health("g1", populated=True, confidence=0.9, gate_critical=True),
            _make_field_health("g2", populated=False, gate_critical=True),
            _make_field_health("g3", populated=False, gate_critical=True),
        ]
        result = compute_readiness_score_v2(fields, _make_domain_spec())

        assert result.overall_score == round((1 / 3) * 60, 2)
        assert result.overall_score <= 30
        assert result.grade == "F"
        assert result.blocking_reason == "gate_critical_fields_missing"
        assert result.recommended_action == "enrich_gates_first"
        assert self._score_at(0.49) == 29.4

    def test_blocked_score_scale_flag_off_restores_gate_score_times_100(self, monkeypatch):
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "health_blocked_score_scale_enabled", False)
        assert self._score_at(0.49) == 49.0
        assert self._score_at(0.50) == 40.0

    def test_get_grade_boundaries(self):
        assert get_grade(0.95) == "A"
        assert get_grade(0.85) == "B"
        assert get_grade(0.75) == "C"
        assert get_grade(0.65) == "D"
        assert get_grade(0.45) == "F"

    def test_staleness_penalty_no_stale_fields(self):
        fields = [_make_field_health("f1", staleness_days=None)]
        assert compute_staleness_penalty(fields) == 0.0

    def test_staleness_penalty_old_fields(self):
        fields = [
            _make_field_health("f1", staleness_days=365),
            _make_field_health("f2", staleness_days=0),
        ]
        penalty = compute_staleness_penalty(fields)
        assert penalty == pytest.approx(0.5, abs=0.01)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GAP PRIORITIZER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGapPrioritizer:
    """Tests for gap_prioritizer.prioritize_gaps_v2."""

    def test_gate_critical_first(self):
        """Gate-critical gaps always rank above non-gate gaps."""
        fields = [
            _make_field_health("scoring_field", populated=False, scoring_weight=0.9),
            _make_field_health("gate_field", populated=False, gate_critical=True, scoring_weight=0.1),
        ]
        spec = _make_domain_spec()
        targets = prioritize_gaps_v2(fields, spec)

        assert len(targets) == 2
        assert targets[0].field_name == "gate_field"
        assert targets[0].is_gate_critical is True

    def test_scoring_weight_ordering(self):
        """Higher scoring weight → higher priority (no gates)."""
        fields = [
            _make_field_health("low", populated=False, scoring_weight=0.1),
            _make_field_health("high", populated=False, scoring_weight=0.9),
        ]
        spec = _make_domain_spec()
        targets = prioritize_gaps_v2(fields, spec)

        assert targets[0].field_name == "high"
        assert targets[1].field_name == "low"

    def test_inference_unlock_bonus(self):
        """Fields that unlock inference rules get priority boost."""
        fields = [
            _make_field_health("unlock_field", populated=False, scoring_weight=0.1),
            _make_field_health("normal_field", populated=False, scoring_weight=0.1),
            _make_field_health("populated_dep", populated=True),
        ]
        inference_rules = [
            {
                "rule_id": "r1",
                "input_fields": ["unlock_field", "populated_dep"],
                "output_field": "derived_field",
            }
        ]
        spec = _make_domain_spec(gates=["derived_field"])
        targets = prioritize_gaps_v2(fields, spec, inference_rules=inference_rules)

        # unlock_field should rank higher due to inference unlock bonus
        assert targets[0].field_name == "unlock_field"
        assert "r1" in targets[0].unlocks_rules

    def test_populated_fields_excluded(self):
        """Populated fields are not included in gap list."""
        fields = [
            _make_field_health("populated", populated=True),
            _make_field_health("missing", populated=False),
        ]
        spec = _make_domain_spec()
        targets = prioritize_gaps_v2(fields, spec)

        assert len(targets) == 1
        assert targets[0].field_name == "missing"

    def test_is_field_populated_helper(self):
        fields = [_make_field_health("f1", populated=True), _make_field_health("f2", populated=False)]
        assert is_field_populated("f1", fields) is True
        assert is_field_populated("f2", fields) is False
        assert is_field_populated("f3", fields) is False

    def test_estimate_research_cost(self):
        gap = _make_field_health("gate", populated=False, gate_critical=True, scoring_weight=0.5)
        spec = _make_domain_spec()
        cost = estimate_research_cost(gap, spec)
        assert cost > 800  # Gate-critical base + complexity


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MATCH QUALITY DELTA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMatchQualityDelta:
    """Tests for enrichment_trigger.measure_health_impact."""

    def test_basic_delta(self):
        before = EntityHealth(entity_id="e1", domain="test", readiness_score=40.0, grade="F", gate_completeness=0.3)
        after = EntityHealth(entity_id="e1", domain="test", readiness_score=80.0, grade="B", gate_completeness=0.9)

        delta = measure_health_impact("e1", "test", before, after, enrichment_cost_usd=0.5)

        assert isinstance(delta, MatchQualityDelta)
        assert delta.readiness_improvement == 40.0
        assert delta.gate_pass_improvement == pytest.approx(0.6, abs=0.01)
        assert delta.roi is not None
        assert delta.roi > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DOMAIN FIELD MAP — gate-critical set (RU-109 / CEG-COR-045)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _plasticos_with_window_gate():
    """Checked-in plasticos pack plus a TEMPORALRANGE gate reading two Facility window bounds."""
    from engine.config.loader import DomainPackLoader
    from engine.config.schema import GateSpec, GateType, PropertySpec, PropertyType

    spec = DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT)).load_domain("plasticos")
    nodes = []
    for node in spec.ontology.nodes:
        if node.label == "Facility":
            extra = [
                PropertySpec(name="available_from", type=PropertyType.DATETIME),
                PropertySpec(name="available_until", type=PropertyType.DATETIME),
            ]
            nodes.append(node.model_copy(update={"properties": [*node.properties, *extra]}))
        else:
            nodes.append(node)
    window_gate = GateSpec(
        name="availability_window",
        type=GateType.TEMPORALRANGE,
        candidateprop_start="available_from",
        candidateprop_end="available_until",
        queryparam="pickup_date",
    )
    return spec.model_copy(
        update={
            "ontology": spec.ontology.model_copy(update={"nodes": nodes}),
            "gates": [*spec.gates, window_gate],
        }
    )


_RANGE_AND_WINDOW_BOUNDS = ("min_density", "max_density", "min_mfi", "max_mfi", "available_from", "available_until")


class TestDomainFieldMapGateCritical:
    def test_range_and_window_bounds_are_gate_critical(self):
        from engine.health.domain_field_mapper import build_field_map

        field_map = build_field_map(_plasticos_with_window_gate(), "Facility")
        for name in _RANGE_AND_WINDOW_BOUNDS:
            assert field_map.fields[name].gate_critical is True, name
            assert field_map.fields[name].impact_tier == "critical", name
        # candidateprop-only gates unchanged
        assert field_map.fields["contamination_tolerance"].gate_critical is True
        assert field_map.fields["name"].gate_critical is False

    def test_entity_missing_range_bounds_is_not_gate_complete(self):
        from engine.health.field_analyzer import analyze_entity_fields

        spec = _plasticos_with_window_gate()
        entity = {
            "facility_id": "fac-1",
            "contamination_tolerance": 0.05,
            "food_grade_certified": True,
            "capacity_tons_month": 400,
        }
        field_health = analyze_entity_fields(entity, spec, "Facility")
        readiness = compute_readiness_score_v2(field_health, spec)
        critical_gaps = [f.field_name for f in field_health if f.is_gate_critical and not f.is_populated]

        assert readiness.gate_completeness < 1.0
        for name in _RANGE_AND_WINDOW_BOUNDS:
            assert name in critical_gaps, name

    def test_range_gate_fields_flag_off_restores_candidateprop_only(self, monkeypatch):
        from engine.config.settings import settings
        from engine.health.domain_field_mapper import build_field_map

        monkeypatch.setattr(settings, "health_range_gate_fields_enabled", False)
        field_map = build_field_map(_plasticos_with_window_gate(), "Facility")
        for name in _RANGE_AND_WINDOW_BOUNDS:
            assert field_map.fields[name].gate_critical is False, name
            assert field_map.fields[name].impact_tier == "low", name
        assert field_map.fields["contamination_tolerance"].gate_critical is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MODEL SERIALIZATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModels:
    """Basic model construction and serialization tests."""

    def test_field_health_defaults(self):
        fh = FieldHealth(field_name="test", is_populated=True)
        assert fh.confidence is None
        assert fh.impact_tier == "low"

    def test_readiness_score_serialization(self):
        rs = ReadinessScore(overall_score=85.5, grade="B", gate_completeness=1.0, scoring_dimension_coverage=0.8)
        d = rs.model_dump()
        assert d["overall_score"] == 85.5
        assert d["grade"] == "B"

    def test_entity_health_full(self):
        eh = EntityHealth(
            entity_id="e1",
            domain="test",
            readiness_score=72.5,
            grade="C",
            critical_gaps=["field_a"],
            enrichment_targets=[EnrichmentTarget(field_name="field_a", priority_score=1000, is_gate_critical=True)],
        )
        assert len(eh.enrichment_targets) == 1
        assert eh.next_enrichment_priority == "normal"
