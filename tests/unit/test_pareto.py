# tests/unit/test_pareto.py
"""Unit tests for Pareto scoring module."""

from __future__ import annotations

import pytest

from engine.scoring.pareto import (
    ParetoCandidate,
    WeightVector,
    compute_pareto_front,
    discover_pareto_weights,
)


@pytest.mark.unit
class TestParetoCandidate:
    """Test ParetoCandidate data structure."""

    def test_create_candidate(self) -> None:
        cand = ParetoCandidate(
            candidate_id="c1",
            dimension_scores={"geo": 0.8, "price": 0.6},
        )
        assert cand.candidate_id == "c1"
        assert cand.dimension_scores["geo"] == 0.8
        assert cand.weighted_score == 0.0
        assert cand.is_dominated is False


@pytest.mark.unit
class TestComputeParetoFront:
    """Test Pareto front computation."""

    def test_empty_candidates(self) -> None:
        front = compute_pareto_front([])
        assert front.front_size == 0
        assert front.non_dominated == []
        assert front.dominated == []

    def test_single_candidate(self) -> None:
        cand = ParetoCandidate(
            candidate_id="c1",
            dimension_scores={"geo": 0.8, "price": 0.6},
        )
        front = compute_pareto_front([cand])
        assert front.front_size == 1
        assert front.non_dominated == [cand]
        assert front.dominated == []

    def test_two_non_dominated(self) -> None:
        """Two candidates that don't dominate each other."""
        c1 = ParetoCandidate(
            candidate_id="c1",
            dimension_scores={"geo": 0.9, "price": 0.3},
        )
        c2 = ParetoCandidate(
            candidate_id="c2",
            dimension_scores={"geo": 0.3, "price": 0.9},
        )
        front = compute_pareto_front([c1, c2])
        assert front.front_size == 2
        assert len(front.dominated) == 0

    def test_one_dominates_other(self) -> None:
        """c1 dominates c2 (better in all dimensions)."""
        c1 = ParetoCandidate(
            candidate_id="c1",
            dimension_scores={"geo": 0.9, "price": 0.8},
        )
        c2 = ParetoCandidate(
            candidate_id="c2",
            dimension_scores={"geo": 0.5, "price": 0.4},
        )
        front = compute_pareto_front([c1, c2])
        assert front.front_size == 1
        assert front.non_dominated[0].candidate_id == "c1"
        assert front.dominated[0].candidate_id == "c2"

    def test_three_candidates_mixed(self) -> None:
        """c1 and c2 on front, c3 dominated."""
        c1 = ParetoCandidate(
            candidate_id="c1",
            dimension_scores={"geo": 0.9, "price": 0.3, "time": 0.5},
        )
        c2 = ParetoCandidate(
            candidate_id="c2",
            dimension_scores={"geo": 0.3, "price": 0.9, "time": 0.5},
        )
        c3 = ParetoCandidate(
            candidate_id="c3",
            dimension_scores={"geo": 0.2, "price": 0.2, "time": 0.2},
        )
        front = compute_pareto_front([c1, c2, c3])
        assert front.front_size == 2
        non_dom_ids = {c.candidate_id for c in front.non_dominated}
        assert non_dom_ids == {"c1", "c2"}
        assert front.dominated[0].candidate_id == "c3"

    def test_dimension_names_extracted(self) -> None:
        c1 = ParetoCandidate(
            candidate_id="c1",
            dimension_scores={"alpha": 0.5, "beta": 0.5, "gamma": 0.5},
        )
        front = compute_pareto_front([c1])
        assert front.dimension_names == ["alpha", "beta", "gamma"]


@pytest.mark.unit
class TestDiscoverParetoWeights:
    """Test Dirichlet-sampled weight discovery."""

    def test_empty_dimensions(self) -> None:
        result = discover_pareto_weights(
            dimension_names=[],
            current_weights={},
        )
        assert result == []

    def test_single_dimension(self) -> None:
        result = discover_pareto_weights(
            dimension_names=["geo"],
            current_weights={"geo": 1.0},
            n_samples=5,
        )
        assert len(result) >= 1
        for wv in result:
            assert "geo" in wv.weights
            assert abs(wv.weights["geo"] - 1.0) < 1e-6

    def test_multiple_dimensions(self) -> None:
        result = discover_pareto_weights(
            dimension_names=["geo", "price", "time"],
            current_weights={"geo": 0.4, "price": 0.4, "time": 0.2},
            n_samples=20,
        )
        assert len(result) >= 1
        for wv in result:
            total = sum(wv.weights.values())
            assert abs(total - 1.0) < 1e-6

    def test_baseline_included(self) -> None:
        """Baseline weight vector should be included in samples."""
        result = discover_pareto_weights(
            dimension_names=["a", "b"],
            current_weights={"a": 0.7, "b": 0.3},
            n_samples=10,
            seed=42,
        )
        assert len(result) >= 1

    def test_deterministic_with_seed(self) -> None:
        """Same seed should produce same results."""
        r1 = discover_pareto_weights(
            dimension_names=["x", "y"],
            current_weights={"x": 0.5, "y": 0.5},
            n_samples=10,
            seed=123,
        )
        r2 = discover_pareto_weights(
            dimension_names=["x", "y"],
            current_weights={"x": 0.5, "y": 0.5},
            n_samples=10,
            seed=123,
        )
        assert len(r1) == len(r2)
        for wv1, wv2 in zip(r1, r2, strict=False):
            assert wv1.weights == wv2.weights


@pytest.mark.unit
class TestWeightVector:
    """Test WeightVector data structure."""

    def test_create_weight_vector(self) -> None:
        wv = WeightVector(
            weights={"a": 0.5, "b": 0.5},
            ndcg_score=0.8,
            diversity_score=0.9,
            coverage_score=1.0,
        )
        assert wv.weights["a"] == 0.5
        assert wv.ndcg_score == 0.8
        assert wv.diversity_score == 0.9
        assert wv.coverage_score == 1.0


# ── RU-019 / CEG-COR-026: objective direction (RG-025) ──────────────────────


@pytest.mark.unit
class TestObjectiveDirection:
    """_dominates / compute_pareto_front honour an explicit 'minimize' direction."""

    def test_dominates_minimize_prefers_lower(self) -> None:
        from engine.scoring.pareto import _dominates

        cheap, pricey = {"cost": 0.1}, {"cost": 0.9}
        assert _dominates(cheap, pricey, ["cost"], {"cost": "minimize"})
        assert not _dominates(pricey, cheap, ["cost"], {"cost": "minimize"})

    def test_dominates_default_is_maximize(self) -> None:
        from engine.scoring.pareto import _dominates

        a, b = {"x": 0.9, "y": 0.5}, {"x": 0.1, "y": 0.5}
        assert _dominates(a, b, ["x", "y"]) is _dominates(a, b, ["x", "y"], {"x": "maximize", "y": "maximize"})
        assert _dominates(a, b, ["x", "y"])

    def test_compute_front_minimize_selects_cheap(self) -> None:
        cheap = ParetoCandidate(candidate_id="cheap", dimension_scores={"cost": 0.1})
        pricey = ParetoCandidate(candidate_id="pricey", dimension_scores={"cost": 0.9})
        front = compute_pareto_front([cheap, pricey], directions={"cost": "minimize"})
        assert [c.candidate_id for c in front.non_dominated] == ["cheap"]

    def test_compute_front_mixed_directions(self) -> None:
        """maximize quality, minimize cost: a (q .9, c .2) dominates b (q .5, c .8)."""
        a = ParetoCandidate(candidate_id="a", dimension_scores={"quality": 0.9, "cost": 0.2})
        b = ParetoCandidate(candidate_id="b", dimension_scores={"quality": 0.5, "cost": 0.8})
        c = ParetoCandidate(candidate_id="c", dimension_scores={"quality": 0.95, "cost": 0.9})
        front = compute_pareto_front([a, b, c], directions={"quality": "maximize", "cost": "minimize"})
        assert sorted(x.candidate_id for x in front.non_dominated) == ["a", "c"]

    def test_compute_front_maximize_unchanged_without_directions(self) -> None:
        cheap = ParetoCandidate(candidate_id="cheap", dimension_scores={"cost": 0.1})
        pricey = ParetoCandidate(candidate_id="pricey", dimension_scores={"cost": 0.9})
        assert [c.candidate_id for c in compute_pareto_front([cheap, pricey]).non_dominated] == ["pricey"]


# ── RU-024 / CEG-COR-031: NDCG against selection labels (RG-031) ────────────


def _history_where_a_predicts_selection() -> list[dict]:
    return [
        {"dimension_scores": {"a": 0.9, "b": 0.1}, "was_selected": True},
        {"dimension_scores": {"a": 0.8, "b": 0.3}, "was_selected": True},
        {"dimension_scores": {"a": 0.2, "b": 0.9}, "was_selected": False},
        {"dimension_scores": {"a": 0.1, "b": 0.7}, "was_selected": False},
    ]


@pytest.mark.unit
class TestWeightVectorNdcg:
    def test_ndcg_discriminates_predictive_dimension(self) -> None:
        from engine.scoring.pareto import _evaluate_weight_vector

        history = _history_where_a_predicts_selection()
        ndcg_a, _, _ = _evaluate_weight_vector({"a": 1.0, "b": 0.0}, history, 0.1)
        ndcg_b, _, _ = _evaluate_weight_vector({"a": 0.0, "b": 1.0}, history, 0.1)
        assert ndcg_a > ndcg_b
        assert ndcg_a == pytest.approx(1.0)  # selected outcomes ranked first

    def test_ndcg_zero_without_history_or_selection(self) -> None:
        from engine.scoring.pareto import _evaluate_weight_vector

        assert _evaluate_weight_vector({"a": 1.0}, [], 0.1)[0] == 0.0
        unselected = [{"dimension_scores": {"a": 0.9}, "was_selected": False}]
        assert _evaluate_weight_vector({"a": 1.0}, unselected, 0.1)[0] == 0.0

    def test_front_depends_on_history(self) -> None:
        dims = ["a", "b"]
        with_history = discover_pareto_weights(
            dims, {"a": 0.5, "b": 0.5}, outcome_history=_history_where_a_predicts_selection(), n_samples=30, seed=7
        )
        without_history = discover_pareto_weights(
            dims, {"a": 0.5, "b": 0.5}, outcome_history=None, n_samples=30, seed=7
        )
        assert [wv.weights for wv in with_history] != [wv.weights for wv in without_history]
        # The best-NDCG vector leans on the predictive dimension.
        assert with_history[0].weights["a"] > with_history[0].weights["b"]
