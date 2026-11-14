# tests/unit/test_pareto_ensemble.py
"""Unit tests for Pareto ensemble module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.config.settings import settings
from engine.kge.ensemble import EnsembleResult, VariantScore
from engine.kge.pareto_ensemble import (
    ParetoEnsembleResult,
    _min_confidence,
    _weight_entropy,
)


@pytest.fixture(autouse=True)
def _unparked(monkeypatch: pytest.MonkeyPatch) -> None:
    """CEG-KGE-PARK: prove the parked mechanism under an explicit un-park.

    The default refusal is asserted in tests/unit/test_kge_parked.py.
    """
    monkeypatch.setattr(settings, "parked_subsystems_enabled", True)


@pytest.mark.unit
class TestHelperFunctions:
    """Test helper functions for Pareto ensemble."""

    def test_min_confidence_empty(self) -> None:
        result = MagicMock(spec=EnsembleResult)
        result.component_scores = []
        assert _min_confidence(result) == 0.0

    def test_min_confidence_single(self) -> None:
        vs = MagicMock(spec=VariantScore)
        vs.confidence = 0.8
        result = MagicMock(spec=EnsembleResult)
        result.component_scores = [vs]
        assert _min_confidence(result) == 0.8

    def test_min_confidence_multiple(self) -> None:
        vs1 = MagicMock(spec=VariantScore)
        vs1.confidence = 0.9
        vs2 = MagicMock(spec=VariantScore)
        vs2.confidence = 0.6
        vs3 = MagicMock(spec=VariantScore)
        vs3.confidence = 0.75
        result = MagicMock(spec=EnsembleResult)
        result.component_scores = [vs1, vs2, vs3]
        assert _min_confidence(result) == 0.6

    def test_weight_entropy_empty(self) -> None:
        result = MagicMock(spec=EnsembleResult)
        result.weights = {}
        assert _weight_entropy(result) == 0.0

    def test_weight_entropy_single(self) -> None:
        result = MagicMock(spec=EnsembleResult)
        result.weights = {"a": 1.0}
        assert _weight_entropy(result) == 0.0

    def test_weight_entropy_uniform(self) -> None:
        result = MagicMock(spec=EnsembleResult)
        result.weights = {"a": 0.5, "b": 0.5}
        entropy = _weight_entropy(result)
        assert abs(entropy - 1.0) < 1e-6


@pytest.mark.unit
class TestParetoEnsembleResult:
    """Test ParetoEnsembleResult data structure."""

    def test_create_result(self) -> None:
        r1 = MagicMock(spec=EnsembleResult)
        r1.final_score = 0.9
        r1.component_scores = []
        r1.weights = {"a": 0.5, "b": 0.5}

        result = ParetoEnsembleResult(
            all_results=[r1],
            pareto_front=[r1],
        )
        assert len(result.all_results) == 1
        assert len(result.pareto_front) == 1

    def test_select_by_priority_accuracy(self) -> None:
        r1 = MagicMock(spec=EnsembleResult)
        r1.final_score = 0.7
        r2 = MagicMock(spec=EnsembleResult)
        r2.final_score = 0.9

        result = ParetoEnsembleResult(
            all_results=[r1, r2],
            pareto_front=[r1, r2],
        )
        selected = result.select_by_priority("accuracy")
        assert selected.final_score == 0.9

    def test_select_by_priority_robustness(self) -> None:
        vs1 = MagicMock(spec=VariantScore)
        vs1.confidence = 0.5
        vs2 = MagicMock(spec=VariantScore)
        vs2.confidence = 0.8

        r1 = MagicMock(spec=EnsembleResult)
        r1.final_score = 0.9
        r1.component_scores = [vs1]
        r1.weights = {}

        r2 = MagicMock(spec=EnsembleResult)
        r2.final_score = 0.7
        r2.component_scores = [vs2]
        r2.weights = {}

        result = ParetoEnsembleResult(
            all_results=[r1, r2],
            pareto_front=[r1, r2],
        )
        selected = result.select_by_priority("robustness")
        assert selected.component_scores[0].confidence == 0.8

    def test_select_by_priority_empty_raises(self) -> None:
        result = ParetoEnsembleResult(
            all_results=[],
            pareto_front=[],
        )
        with pytest.raises(ValueError, match="No ensemble results"):
            result.select_by_priority("accuracy")

    def test_select_by_priority_unknown_raises(self) -> None:
        r1 = MagicMock(spec=EnsembleResult)
        r1.final_score = 0.9
        result = ParetoEnsembleResult(
            all_results=[r1],
            pareto_front=[r1],
        )
        with pytest.raises(ValueError, match="Unknown priority"):
            result.select_by_priority("unknown")


@pytest.mark.unit
class TestParetoFrontNotForcedToRankAggregation:
    """RG-147 (RU-112 / CEG-COR-048): rank aggregation no longer fuses every
    input to 1.0, so it cannot dominate every other strategy by construction."""

    def test_low_scores_front_is_not_only_borda(self) -> None:
        from unittest.mock import patch

        from engine.kge.pareto_ensemble import ParetoEnsembleController

        scores = [
            VariantScore("v1", "r", score=0.01, confidence=0.9),
            VariantScore("v2", "r", score=0.02, confidence=0.9),
        ]
        with patch("engine.kge.ensemble.settings") as mock_settings:
            mock_settings.kge_enabled = True
            mock_settings.kge_confidence_threshold = 0.0
            result = ParetoEnsembleController().predict_pareto(scores)

        assert len(result.all_results) == 3
        borda = next(r for r in result.all_results if r.fusion_strategy == "borda")
        assert borda.final_score <= 0.02
        assert [r.fusion_strategy for r in result.pareto_front] != ["borda"]
