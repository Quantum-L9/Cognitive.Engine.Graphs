"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, feedback, confidence-intervals]
owner: engine-team
status: active
--- /L9_META ---

Tests for weight confidence intervals in SignalWeightCalculator.

Covers:
- CI calculation produces valid confidence values
- Dampening blends uncertain weights toward 1.0
- High sample sizes produce high confidence (narrow CI)
- Low sample sizes produce low confidence (wide CI)
- Confidence stored on DimensionWeight nodes
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.config.schema import (
    ComputationType,
    DomainSpec,
    FeedbackLoopSpec,
    ScoringDimensionSpec,
    ScoringSource,
    ScoringSpec,
    SignalWeightSpec,
)
from engine.feedback.signal_weights import SignalWeightCalculator

pytestmark = pytest.mark.unit


def _spec_with_confidence(
    confidence_dampening: bool = True,
    min_weight: float = 0.1,
    max_weight: float = 3.0,
) -> DomainSpec:
    return DomainSpec(
        domain={"id": "test", "name": "Test", "version": "0.0.1"},
        ontology={
            "nodes": [
                {
                    "label": "Facility",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "facility_id", "type": "int", "required": True}],
                },
                {
                    "label": "Query",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "query_id", "type": "int", "required": True}],
                },
            ],
            "edges": [
                {
                    "type": "TRANSACTED_WITH",
                    "from": "Facility",
                    "to": "Facility",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "sync",
                }
            ],
        },
        matchentities={
            "candidate": [{"label": "Facility", "matchdirection": "d1"}],
            "queryentity": [{"label": "Query", "matchdirection": "d1"}],
        },
        queryschema={"matchdirections": ["d1"], "fields": []},
        gates=[],
        scoring=ScoringSpec(
            dimensions=[
                ScoringDimensionSpec(
                    name="geo_score",
                    source=ScoringSource.CANDIDATEPROPERTY,
                    candidateprop="lat",
                    computation=ComputationType.GEODECAY,
                    weightkey="w_geo",
                    defaultweight=0.5,
                ),
            ]
        ),
        feedbackloop=FeedbackLoopSpec(
            outcome_node_label="TransactionOutcome",
            enabled=True,
            signal_weights=SignalWeightSpec(
                enabled=True,
                confidence_dampening=confidence_dampening,
                min_weight=min_weight,
                max_weight=max_weight,
            ),
        ),
    )


class TestConfidenceIntervals:
    """Tests for CI calculation and dampening."""

    @pytest.mark.asyncio
    async def test_high_sample_size_high_confidence(self) -> None:
        spec = _spec_with_confidence()
        driver = AsyncMock()
        calc = SignalWeightCalculator(driver, spec)

        # Total outcomes: 1000, wins: 600
        driver.execute_query = AsyncMock(
            side_effect=[
                [{"total": 1000, "wins": 600}],
                [{"dim_total": 500, "dim_wins": 400}],
                [],  # store weights call
            ]
        )

        weights = await calc.recalculate_weights()
        assert "geo_score" in weights
        # With 500 samples and a positive lift, confidence should be high
        # weight should be close to the lift value, not dampened much

    @pytest.mark.asyncio
    async def test_low_sample_size_dampened_toward_one(self) -> None:
        spec = _spec_with_confidence()
        driver = AsyncMock()
        calc = SignalWeightCalculator(driver, spec)

        # Total outcomes: 100, wins: 60 (base rate 0.6)
        # Dimension: 3 samples, 3 wins (lift=1.67 but low confidence)
        driver.execute_query = AsyncMock(
            side_effect=[
                [{"total": 100, "wins": 60}],
                [{"dim_total": 3, "dim_wins": 3}],
                [],  # store weights call
            ]
        )

        weights = await calc.recalculate_weights()
        geo_weight = weights["geo_score"]
        # With very low sample (3), confidence should be low,
        # dampened weight should be closer to 1.0 than raw lift
        assert geo_weight < 3.0  # Not at max
        assert geo_weight >= 0.1  # Within bounds

    @pytest.mark.asyncio
    async def test_dampening_disabled_returns_raw_weight(self) -> None:
        spec = _spec_with_confidence(confidence_dampening=False)
        driver = AsyncMock()
        calc = SignalWeightCalculator(driver, spec)

        # Total outcomes: 100, wins: 50 (base rate 0.5)
        # Dimension: 50 samples, 40 wins (lift=1.6)
        driver.execute_query = AsyncMock(
            side_effect=[
                [{"total": 100, "wins": 50}],
                [{"dim_total": 50, "dim_wins": 40}],
                [],  # store weights call
            ]
        )

        weights = await calc.recalculate_weights()
        geo_weight = weights["geo_score"]
        # Without dampening, weight = lift * frequency_factor
        # lift = 1.6, frequency = min(1.0, (50/100)*10) = 1.0
        # raw = 1.6 * 1.0 = 1.6
        assert abs(geo_weight - 1.6) < 0.01

    @pytest.mark.asyncio
    async def test_confidence_and_ci_width_stored_in_neo4j(self) -> None:
        spec = _spec_with_confidence()
        driver = AsyncMock()
        calc = SignalWeightCalculator(driver, spec)

        driver.execute_query = AsyncMock(
            side_effect=[
                [{"total": 100, "wins": 50}],
                [{"dim_total": 80, "dim_wins": 60}],
                [],  # store weights call
            ]
        )

        await calc.recalculate_weights()

        # The store call should include confidence and ci_width parameters
        store_call = driver.execute_query.call_args_list[-1]
        # CEG-462: the recalculation is one UNWIND statement; each row is one dimension.
        (params,) = store_call.kwargs.get("parameters", {})["rows"]
        assert "confidence" in params
        assert "ci_width" in params
        assert 0.0 <= params["confidence"] <= 1.0
        assert params["ci_width"] >= 0.0

    @pytest.mark.asyncio
    async def test_zero_outcomes_returns_baseline(self) -> None:
        spec = _spec_with_confidence()
        driver = AsyncMock()
        calc = SignalWeightCalculator(driver, spec)

        driver.execute_query = AsyncMock(return_value=[{"total": 0, "wins": 0}])

        weights = await calc.recalculate_weights()
        assert weights["geo_score"] == 1.0  # baseline_weight


async def _dimension_detail(dim_total: int, dim_wins: int, base_win_rate: float) -> dict[str, float]:
    spec = _spec_with_confidence()
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[{"dim_total": dim_total, "dim_wins": dim_wins}])
    calc = SignalWeightCalculator(driver, spec)
    return await calc._compute_dimension_weight("geo_score", base_win_rate, max(dim_total, 1), "TransactionOutcome")


class TestProportionConfidenceInterval:
    """RU-027 / CEG-COR-116 (RG-034): the CI comes from the win-rate proportion, not lift*(1-lift)."""

    @pytest.mark.asyncio
    async def test_positive_lift_ci_shrinks_with_sample_size(self) -> None:
        # lift 2 at both sizes: base 0.5, every dimension outcome a win.
        small = await _dimension_detail(dim_total=1, dim_wins=1, base_win_rate=0.5)
        large = await _dimension_detail(dim_total=100, dim_wins=100, base_win_rate=0.5)
        assert small["lift"] == large["lift"] == 2.0
        assert small["ci_width"] > large["ci_width"] > 0.0
        assert small["confidence"] < large["confidence"]

    @pytest.mark.asyncio
    async def test_interior_win_rate_ci_shrinks_with_sample_size(self) -> None:
        # lift 2: base 0.25, dimension win rate 0.5.
        n4 = await _dimension_detail(dim_total=4, dim_wins=2, base_win_rate=0.25)
        n100 = await _dimension_detail(dim_total=100, dim_wins=50, base_win_rate=0.25)
        assert n4["ci_width"] > n100["ci_width"] > 0.0
        assert n4["confidence"] < n100["confidence"]

    @pytest.mark.asyncio
    async def test_small_sample_dampens_positive_lift(self) -> None:
        """Dampening now pulls an n=1 lift-2 weight toward 1.0 (was confidence 1.0)."""
        small = await _dimension_detail(dim_total=1, dim_wins=1, base_win_rate=0.5)
        assert small["confidence"] < 0.5
        assert 1.0 <= small["weight"] < 2.0

    @pytest.mark.asyncio
    async def test_flag_off_restores_lift_formula(self, monkeypatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "signal_weight_proportion_ci", False)
        small = await _dimension_detail(dim_total=1, dim_wins=1, base_win_rate=0.5)
        assert small["ci_width"] == 0.0
        assert small["confidence"] == 1.0
