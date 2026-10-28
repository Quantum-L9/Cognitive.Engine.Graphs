"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, feedback, wave2]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for W2-02: Outcome Feedback Loop.
Tests discriminability computation, weight proposals, and apply logic.
"""

from __future__ import annotations

import pytest

from engine.config.settings import settings
from engine.scoring.feedback import MIN_OUTCOMES, WEIGHT_NUDGE, OutcomeFeedback


@pytest.fixture(params=[True, False], ids=["unified", "legacy"])
def vocab(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """(positive, negative, neither) outcome values for each outcome_schema_unified state.

    RU-006 / CEG-COR-015: on, OutcomeFeedback reads the canonical
    success|failure|partial record; off, the legacy positive|negative|neutral.
    """
    monkeypatch.setattr(settings, "outcome_schema_unified", request.param)
    return ("success", "failure", "partial") if request.param else ("positive", "negative", "neutral")


@pytest.mark.unit
class TestOutcomeFeedbackCompute:
    """Test OutcomeFeedback.compute_feedback()."""

    def test_insufficient_data(self, vocab):
        pos, _, _ = vocab
        outcomes = [{"outcome": pos, "dimension_scores": {"geo": 0.8}}]
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        assert result["sufficient_data"] is False
        assert result["sample_count"] == 1
        assert result["proposed_weights"] == {}

    def test_positive_only_is_insufficient(self, vocab):
        # RU-025 / CEG-COR-032: formerly test_sufficient_positive_only, which
        # asserted sufficient_data True for rows of one class — the defect
        # (no negative evidence read as a mean of 0.0, giving +0.01 nudges).
        pos, _, _ = vocab
        outcomes = [{"outcome": pos, "dimension_scores": {"geo": 0.9, "structural": 0.7}} for _ in range(MIN_OUTCOMES)]
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        assert result["sufficient_data"] is False
        assert result["proposed_weights"] == {}
        assert result["sample_count"] == MIN_OUTCOMES
        assert "geo" in result["dimension_analysis"]
        assert "structural" in result["dimension_analysis"]
        geo = result["dimension_analysis"]["geo"]
        assert geo["positive_count"] == MIN_OUTCOMES
        assert geo["negative_count"] == 0
        assert geo["discriminability"] is None
        assert "message" in result

    def test_discriminability_positive_vs_negative(self, vocab):
        pos, neg, _ = vocab
        outcomes = []
        for _ in range(6):
            outcomes.append({"outcome": pos, "dimension_scores": {"geo": 0.9}})
        for _ in range(6):
            outcomes.append({"outcome": neg, "dimension_scores": {"geo": 0.2}})
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        assert result["sufficient_data"] is True
        analysis = result["dimension_analysis"]["geo"]
        assert analysis["positive_mean"] == pytest.approx(0.9, abs=0.01)
        assert analysis["negative_mean"] == pytest.approx(0.2, abs=0.01)
        assert analysis["discriminability"] > 0
        # Nudge should be positive (dimension predicts success)
        assert result["proposed_weights"]["geo"] > 0

    def test_negative_discriminability(self, vocab):
        pos, neg, _ = vocab
        outcomes = []
        for _ in range(6):
            outcomes.append({"outcome": pos, "dimension_scores": {"geo": 0.2}})
        for _ in range(6):
            outcomes.append({"outcome": neg, "dimension_scores": {"geo": 0.8}})
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        # Negative discriminability means the dimension anti-predicts success
        assert result["proposed_weights"]["geo"] < 0

    def test_nudge_capped_at_max(self, vocab):
        pos, neg, _ = vocab
        outcomes = []
        for _ in range(6):
            outcomes.append({"outcome": pos, "dimension_scores": {"d": 100.0}})
        for _ in range(6):
            outcomes.append({"outcome": neg, "dimension_scores": {"d": -100.0}})
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        # Nudge should be capped at WEIGHT_NUDGE
        assert abs(result["proposed_weights"]["d"]) <= WEIGHT_NUDGE + 0.0001

    def test_neutral_outcomes_ignored(self, vocab):
        _, _, neu = vocab
        outcomes = [{"outcome": neu, "dimension_scores": {"geo": 0.5}} for _ in range(MIN_OUTCOMES)]
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        # RU-025: neutral-only rows give no dimension two classes to compare
        # (this asserted sufficient_data True before the fix).
        assert result["sufficient_data"] is False
        # Neutral outcomes don't contribute to positive or negative
        assert result["proposed_weights"] == {}

    def test_invalid_outcome_items_skipped(self, vocab):
        pos, neg, _ = vocab
        outcomes = [
            "not a dict",
            {"outcome": pos},  # missing dimension_scores
            {"outcome": pos, "dimension_scores": "not a dict"},
        ]
        # Pad to minimum with both classes (RU-025: one class alone is not evidence)
        for i in range(MIN_OUTCOMES):
            outcomes.append({"outcome": pos if i % 2 else neg, "dimension_scores": {"geo": 0.5}})
        fb = OutcomeFeedback(outcomes)
        result = fb.compute_feedback()

        assert result["sufficient_data"] is True
        assert result["dimension_analysis"]["geo"]["positive_count"] == MIN_OUTCOMES // 2

    def test_rows_without_dimension_scores_are_insufficient(self, vocab):
        pos, neg, _ = vocab
        outcomes = [{"outcome": pos if i % 2 else neg} for i in range(2 * MIN_OUTCOMES)]
        result = OutcomeFeedback(outcomes).compute_feedback()

        assert result["sufficient_data"] is False
        assert result["proposed_weights"] == {}
        assert result["dimension_analysis"] == {}
        assert "message" in result

    def test_only_dimensions_with_both_classes_get_proposals(self, vocab):
        pos, neg, _ = vocab
        outcomes = [{"outcome": pos, "dimension_scores": {"geo": 0.9, "price": 0.8}} for _ in range(6)]
        outcomes += [{"outcome": neg, "dimension_scores": {"geo": 0.2}} for _ in range(6)]
        result = OutcomeFeedback(outcomes).compute_feedback()

        assert result["sufficient_data"] is True
        assert set(result["proposed_weights"]) == {"geo"}
        assert result["proposed_weights"]["geo"] > 0
        price = result["dimension_analysis"]["price"]
        assert price["positive_count"] == 6
        assert price["negative_count"] == 0
        assert price["discriminability"] is None
        assert price["proposed_nudge"] is None

    @pytest.mark.usefixtures("vocab")
    def test_empty_outcomes(self):
        fb = OutcomeFeedback([])
        result = fb.compute_feedback()

        assert result["sufficient_data"] is False
        assert result["sample_count"] == 0


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-015")
class TestCanonicalOutcomeVocabulary:
    """RU-006 (RG-007): the analyzer reads the record handle_outcomes writes."""

    def test_success_failure_records_produce_proposals(self):
        outcomes = [{"outcome": "success", "dimension_scores": {"geo": 0.9}} for _ in range(6)]
        outcomes += [{"outcome": "failure", "dimension_scores": {"geo": 0.2}} for _ in range(6)]
        result = OutcomeFeedback(outcomes).compute_feedback()

        assert result["proposed_weights"]["geo"] > 0
        assert result["dimension_analysis"]["geo"]["positive_count"] == 6
        assert result["dimension_analysis"]["geo"]["negative_count"] == 6

    def test_partial_is_neither_class(self):
        outcomes = [{"outcome": "success", "dimension_scores": {"geo": 0.9}} for _ in range(6)]
        outcomes += [{"outcome": "failure", "dimension_scores": {"geo": 0.2}} for _ in range(6)]
        outcomes += [{"outcome": "partial", "dimension_scores": {"geo": 0.0}} for _ in range(6)]
        analysis = OutcomeFeedback(outcomes).compute_feedback()["dimension_analysis"]["geo"]

        assert analysis["positive_mean"] == pytest.approx(0.9)
        assert analysis["negative_mean"] == pytest.approx(0.2)


@pytest.mark.unit
class TestApplyWeights:
    """Test OutcomeFeedback.apply_weights()."""

    def test_apply_positive_delta(self):
        current = {"geo": 0.25, "structural": 0.30}
        proposed = {"geo": 0.02, "structural": -0.01}
        result = OutcomeFeedback.apply_weights(current, proposed)

        assert result["geo"] == pytest.approx(0.27, abs=0.001)
        assert result["structural"] == pytest.approx(0.29, abs=0.001)

    def test_clamp_to_zero(self):
        current = {"geo": 0.01}
        proposed = {"geo": -0.05}
        result = OutcomeFeedback.apply_weights(current, proposed)

        assert result["geo"] == 0.0

    def test_clamp_to_one(self):
        current = {"geo": 0.99}
        proposed = {"geo": 0.05}
        result = OutcomeFeedback.apply_weights(current, proposed)

        assert result["geo"] == 1.0

    def test_missing_delta_is_noop(self):
        current = {"geo": 0.5, "structural": 0.3}
        proposed = {"geo": 0.01}  # no delta for structural
        result = OutcomeFeedback.apply_weights(current, proposed)

        assert result["structural"] == 0.3
        assert result["geo"] == pytest.approx(0.51, abs=0.001)

    def test_empty_inputs(self):
        result = OutcomeFeedback.apply_weights({}, {})
        assert result == {}
