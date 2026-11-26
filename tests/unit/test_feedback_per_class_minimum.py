"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, feedback]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_feedback_per_class_minimum.py
SCR-13 (feedback part): ``OutcomeFeedback.compute_feedback`` proposes a weight
nudge for a dimension only when each outcome class holds at least
``feedback_min_samples_per_class`` scored outcomes. The real-graph proof through
``handle_outcomes`` and admin ``score_feedback`` is
tests/integration/test_feedback_per_class_minimum.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.settings import Settings, settings
from engine.scoring.feedback import OutcomeFeedback

pytestmark = pytest.mark.unit


def _outcomes(successes: int, failures: int, partials: int = 0) -> list[dict[str, Any]]:
    rows = [{"outcome": "success", "dimension_scores": {"reliability": 0.9}} for _ in range(successes)]
    rows += [{"outcome": "failure", "dimension_scores": {"reliability": 0.2}} for _ in range(failures)]
    rows += [{"outcome": "partial", "dimension_scores": {"reliability": 0.5}} for _ in range(partials)]
    return rows


@pytest.fixture
def minimum_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "outcome_schema_unified", True)
    monkeypatch.setattr(settings, "feedback_per_class_minimum_enabled", True)
    monkeypatch.setattr(settings, "feedback_min_samples_per_class", 5)


def test_the_shipped_default_is_on_with_five_per_class() -> None:
    fresh = Settings()
    assert fresh.feedback_per_class_minimum_enabled is True
    assert fresh.feedback_min_samples_per_class == 5


def test_one_sample_of_a_class_is_not_sufficient(minimum_on: None) -> None:
    # 9 successes and 1 failure: ten outcomes, both classes present, one failure.
    result = OutcomeFeedback(_outcomes(9, 1)).compute_feedback()
    assert result["sufficient_data"] is False
    assert result["proposed_weights"] == {}
    analysis = result["dimension_analysis"]["reliability"]
    assert (analysis["positive_count"], analysis["negative_count"]) == (9, 1)
    assert analysis["proposed_nudge"] is None
    assert result["min_samples_per_class"] == 5
    assert "at least 5 scored outcomes of each class" in result["message"]


def test_a_class_one_short_of_the_minimum_is_not_sufficient(minimum_on: None) -> None:
    result = OutcomeFeedback(_outcomes(5, 4, partials=1)).compute_feedback()
    assert result["sufficient_data"] is False
    assert result["dimension_analysis"]["reliability"]["negative_count"] == 4


def test_the_minimum_in_each_class_is_sufficient(minimum_on: None) -> None:
    result = OutcomeFeedback(_outcomes(5, 5)).compute_feedback()
    assert result["sufficient_data"] is True
    # 0.02 * (0.9 - 0.2) = 0.014: the proposal itself is unchanged by the minimum.
    assert result["proposed_weights"] == {"reliability": pytest.approx(0.014)}


def test_the_minimum_is_a_setting(minimum_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "feedback_min_samples_per_class", 2)
    assert OutcomeFeedback(_outcomes(8, 2)).compute_feedback()["sufficient_data"] is True
    assert OutcomeFeedback(_outcomes(9, 1)).compute_feedback()["sufficient_data"] is False


def test_the_minimum_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        Settings(feedback_min_samples_per_class=0)


def test_flag_off_one_sample_per_class_is_sufficient(minimum_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "feedback_per_class_minimum_enabled", False)
    result = OutcomeFeedback(_outcomes(9, 1)).compute_feedback()
    assert result["sufficient_data"] is True  # the legacy defect the flag closes
    assert "min_samples_per_class" not in result
