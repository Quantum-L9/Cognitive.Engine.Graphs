"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, counterfactual, causal]
owner: engine-team
status: active
--- /L9_META ---

SI-26: counterfactual scenarios report co-occurrence support, not causal confidence.

``counterfactual_support_semantics_enabled`` (default off). Off, scenarios and
the stored Cypher are the legacy ones. On, the frequency is ``support`` (with
its count, pool size and basis), ``claim`` is ``co_occurrence``, the outcome is
``untested`` and no ``confidence`` is written.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.causal.counterfactual import CounterfactualGenerator
from engine.config.schema import CounterfactualSpec
from engine.config.settings import settings

pytestmark = pytest.mark.unit

ACTUAL = {
    "active_dimensions": ["alpha", "beta"],
    "dimension_weights": {"alpha": 0.2, "beta": 0.4},
    "gates_passed": [],
    "match_direction": "forward",
    "candidate_count": 5,
    "outcome": "failure",
}

# 4 positives: gamma active in 3, delta in 1; alpha weight > 1.5x actual in 2
# (0.5, 0.31); beta weight > 1.5x actual in none.
POSITIVES = [
    {
        "active_dimensions": ["alpha", "gamma"],
        "dimension_weights": {"alpha": 0.5},
        "gates_passed": [],
        "outcome_id": "p1",
    },
    {
        "active_dimensions": ["alpha", "gamma"],
        "dimension_weights": {"alpha": 0.31},
        "gates_passed": [],
        "outcome_id": "p2",
    },
    {
        "active_dimensions": ["alpha", "gamma", "delta"],
        "dimension_weights": {"alpha": 0.2},
        "gates_passed": [],
        "outcome_id": "p3",
    },
    {
        "active_dimensions": ["alpha", "beta"],
        "dimension_weights": {"beta": 0.5},
        "gates_passed": [],
        "outcome_id": "p4",
    },
]


def _generator(**spec: Any) -> CounterfactualGenerator:
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    params = {"enabled": True, "max_scenarios_per_outcome": 5, "min_confidence": 0.3, "comparison_pool_size": 10}
    params.update(spec)
    return CounterfactualGenerator(CounterfactualSpec(**params), driver, "dom", "Outcome")


def _strip_ids(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in s.items() if k != "scenario_id"} for s in scenarios]


class TestFlagOffIsLegacy:
    def test_default_is_off(self) -> None:
        assert type(settings).model_fields["counterfactual_support_semantics_enabled"].default is False

    def test_scenarios_identical(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", False)
        gen = _generator()
        out = gen._diff_fingerprints(ACTUAL, POSITIVES, "o1")
        assert _strip_ids(out) == [
            {
                "actual_outcome": "o1",
                "counterfactual_outcome": "success",
                "intervention_type": "add_scoring_dimension",
                "confidence": 0.75,
                "key_difference": "gamma",
            },
            # legacy: confidence = share of positives with alpha ACTIVE (4/4),
            # whatever its weight.
            {
                "actual_outcome": "o1",
                "counterfactual_outcome": "success",
                "intervention_type": "change_weight",
                "confidence": 1.0,
                "key_difference": "alpha_weight_increase",
            },
        ]

    @pytest.mark.asyncio
    async def test_generate_uses_legacy_path_and_cypher(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", False)
        gen = _generator()
        gen._graph_driver.execute_query.side_effect = [[ACTUAL], POSITIVES, []]
        out = await gen.generate_for_outcome("o1")
        assert all("confidence" in s and s["counterfactual_outcome"] == "success" for s in out)
        stored = gen._graph_driver.execute_query.call_args_list[-1].kwargs["cypher"]
        assert "confidence: s.confidence" in stored
        assert "support" not in stored


class TestFlagOnSupport:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", True)

    def test_no_confidence_no_asserted_success(self) -> None:
        out = _generator()._diff_fingerprints_support(ACTUAL, POSITIVES, "o1")
        assert out
        for s in out:
            assert "confidence" not in s
            assert s["counterfactual_outcome"] == "untested"
            assert s["claim"] == "co_occurrence"
            assert s["support_basis"] == "share_of_compared_positive_outcomes"
            assert s["compared_positives"] == 4
            assert s["support"] == round(s["support_count"] / 4, 4)

    def test_values(self) -> None:
        out = _strip_ids(_generator()._diff_fingerprints_support(ACTUAL, POSITIVES, "o1"))
        keyed = {(s["intervention_type"], s["key_difference"]): s for s in out}
        assert keyed[("add_scoring_dimension", "gamma")]["support_count"] == 3
        # weight support counts positives whose weight is actually heavier (2 of 4),
        # not positives with the dimension active (4 of 4).
        assert keyed[("change_weight", "alpha_weight_increase")]["support_count"] == 2
        assert keyed[("change_weight", "alpha_weight_increase")]["support"] == 0.5
        # delta (1/4) is below the minimum support 0.3; beta is never heavier.
        assert ("add_scoring_dimension", "delta") not in keyed
        assert ("change_weight", "beta_weight_increase") not in keyed

    def test_min_support_and_cap(self) -> None:
        assert _generator(min_confidence=0.8)._diff_fingerprints_support(ACTUAL, POSITIVES, "o1") == []
        assert len(_generator(max_scenarios_per_outcome=1)._diff_fingerprints_support(ACTUAL, POSITIVES, "o1")) == 1

    def test_empty_pool(self) -> None:
        assert _generator()._diff_fingerprints_support(ACTUAL, [], "o1") == []

    @pytest.mark.asyncio
    async def test_stored_without_confidence(self) -> None:
        gen = _generator()
        gen._graph_driver.execute_query.side_effect = [[ACTUAL], POSITIVES, []]
        out = await gen.generate_for_outcome("o1")
        assert out
        call = gen._graph_driver.execute_query.call_args_list[-1].kwargs
        assert "confidence" not in call["cypher"]
        assert "support: s.support" in call["cypher"]
        assert "claim: s.claim" in call["cypher"]
        assert call["parameters"]["scenarios"] == out
