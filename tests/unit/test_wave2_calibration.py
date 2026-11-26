"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, calibration, wave2]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for W2-01: Score Calibration Framework.
Tests calibration report, forward simulation NDCG, Kendall-tau, drift detection.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from engine.config.schema import CalibrationPair, CalibrationSpec
from engine.scoring.calibration import (
    CalibrationReport,
    DriftReport,
    ForwardSimReport,
    ScoreCalibrator,
)


def _make_domain_spec(calibration: CalibrationSpec | None = None) -> MagicMock:
    spec = MagicMock()
    spec.domain.id = "test_domain"
    spec.calibration = calibration
    return spec


@pytest.mark.unit
class TestCalibrationPairModel:
    """Test CalibrationPair Pydantic validation."""

    def test_valid_pair(self):
        pair = CalibrationPair(node_a="A", node_b="B", expected_score_min=0.3, expected_score_max=0.7)
        assert pair.node_a == "A"
        assert pair.expected_score_min == 0.3

    def test_pair_min_greater_than_max_raises(self):
        with pytest.raises(ValueError, match="expected_score_min"):
            CalibrationPair(node_a="A", node_b="B", expected_score_min=0.8, expected_score_max=0.3)

    def test_pair_with_label(self):
        pair = CalibrationPair(
            node_a="X", node_b="Y", expected_score_min=0.0, expected_score_max=1.0, label="test_pair"
        )
        assert pair.label == "test_pair"


@pytest.mark.unit
class TestRunCalibration:
    """Test ScoreCalibrator.run_calibration()."""

    def test_all_pairs_pass(self):
        pairs = [
            CalibrationPair(node_a="A", node_b="B", expected_score_min=0.5, expected_score_max=0.9),
            CalibrationPair(node_a="C", node_b="D", expected_score_min=0.2, expected_score_max=0.6),
        ]
        actual_scores = {("A", "B"): 0.7, ("C", "D"): 0.4}
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.run_calibration(pairs, actual_scores)

        assert isinstance(report, CalibrationReport)
        assert report.overall_pass is True
        assert report.passed == 2
        assert report.failed == 0

    def test_pair_out_of_range_fails(self):
        pairs = [
            CalibrationPair(node_a="A", node_b="B", expected_score_min=0.5, expected_score_max=0.7),
        ]
        actual_scores = {("A", "B"): 0.3}  # below min
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.run_calibration(pairs, actual_scores)

        assert report.overall_pass is False
        assert report.failed == 1
        assert report.results[0].diff == pytest.approx(0.2, abs=0.001)

    def test_missing_score_fails(self):
        pairs = [
            CalibrationPair(node_a="A", node_b="B", expected_score_min=0.3, expected_score_max=0.7),
        ]
        actual_scores = {}  # no score
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.run_calibration(pairs, actual_scores)

        assert report.overall_pass is False
        assert report.results[0].actual_score is None
        assert report.results[0].diff == -1.0

    def test_empty_pairs_is_not_a_pass(self):
        """D6 / SC-002: nothing checked is no evidence the scores satisfy the spec."""
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.run_calibration([], {})
        assert report.overall_pass is False
        assert report.total_pairs == 0
        assert report.passed == 0
        assert report.failed == 0

    def test_empty_pairs_with_scores_supplied_is_not_a_pass(self):
        """Scores without any declared pair to check them against still pass nothing."""
        report = ScoreCalibrator(_make_domain_spec()).run_calibration([], {("A", "B"): 0.5})
        assert report.overall_pass is False

    def test_score_above_max(self):
        pairs = [
            CalibrationPair(node_a="A", node_b="B", expected_score_min=0.3, expected_score_max=0.5),
        ]
        actual_scores = {("A", "B"): 0.8}  # above max
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.run_calibration(pairs, actual_scores)

        assert report.overall_pass is False
        assert report.results[0].diff == pytest.approx(0.3, abs=0.001)


@pytest.mark.unit
class TestForwardSimulation:
    """Test ScoreCalibrator.forward_simulate()."""

    def test_perfect_ranking(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        abstract = ["A", "B", "C", "D"]
        concrete = ["A", "B", "C", "D"]
        report = calibrator.forward_simulate(abstract, concrete)

        assert isinstance(report, ForwardSimReport)
        assert report.ndcg == pytest.approx(1.0, abs=0.001)
        assert report.kendall_tau == pytest.approx(1.0, abs=0.001)
        assert report.diverged is False
        assert report.misranked_pairs == []

    def test_reversed_ranking(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        abstract = ["A", "B", "C", "D"]
        concrete = ["D", "C", "B", "A"]
        report = calibrator.forward_simulate(abstract, concrete)

        assert report.kendall_tau == pytest.approx(-1.0, abs=0.001)
        assert report.diverged is True
        assert len(report.misranked_pairs) > 0

    def test_partial_overlap(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        abstract = ["A", "B", "C"]
        concrete = ["A", "C", "D"]  # D not in abstract, B missing
        report = calibrator.forward_simulate(abstract, concrete)

        assert 0.0 <= report.ndcg <= 1.0
        assert -1.0 <= report.kendall_tau <= 1.0

    def test_empty_abstract(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.forward_simulate([], ["A", "B"])

        assert report.ndcg == 0.0
        assert report.diverged is True

    def test_empty_concrete(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.forward_simulate(["A", "B"], [])

        assert report.ndcg == 0.0
        assert report.diverged is True

    def test_single_element(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.forward_simulate(["A"], ["A"])

        assert report.ndcg == pytest.approx(1.0, abs=0.001)
        assert report.diverged is False


def _ndcg_at_k_from_first_principles(abstract: list[str], concrete: list[str]) -> float:
    """NDCG@k, k = len(concrete): graded relevance n - position in the abstract ranking.

    Computed independently of engine code: the ideal DCG is the DCG of the
    abstract ranking's own top-k relevances.
    """
    n = len(abstract)
    relevance = {cid: n - i for i, cid in enumerate(abstract)}
    dcg = sum(relevance.get(cid, 0) / math.log2(rank + 2) for rank, cid in enumerate(concrete))
    ideal = sorted(relevance.values(), reverse=True)[: len(concrete)]
    idcg = sum(rel / math.log2(rank + 2) for rank, rel in enumerate(ideal))
    return dcg / idcg


@pytest.mark.unit
class TestForwardSimulationIdealBasis:
    """D6 / SC-002: the ideal DCG comes from the abstract ranking, not from re-sorting the concrete list."""

    def test_single_low_relevance_item_is_diverged(self):
        """[a,b,c,d] vs [d]: re-sorting [d]'s own relevances made it its own ideal (NDCG 1.0)."""
        report = ScoreCalibrator(_make_domain_spec()).forward_simulate(["a", "b", "c", "d"], ["d"])

        # d has relevance 1, the abstract top-1 has relevance 4: NDCG@1 = 1/4.
        assert report.ndcg == pytest.approx(0.25, abs=1e-6)
        assert report.ndcg < 1.0
        assert report.diverged is True

    def test_irrelevant_items_ahead_of_the_top_item(self):
        abstract = ["a", "b", "c", "d"]
        concrete = ["x", "y", "a"]
        report = ScoreCalibrator(_make_domain_spec()).forward_simulate(abstract, concrete)

        # DCG  = 4 / log2(4) = 2.0
        # IDCG = 4 / log2(2) + 3 / log2(3) + 2 / log2(4) = 4 + 1.892789 + 1 = 6.892789
        expected = 2.0 / (4.0 + 3.0 / math.log2(3) + 1.0)
        assert expected == pytest.approx(_ndcg_at_k_from_first_principles(abstract, concrete), abs=1e-12)
        assert expected == pytest.approx(0.2902, abs=1e-4)
        assert report.ndcg == pytest.approx(expected, abs=1e-6)
        assert report.diverged is True

    def test_correct_prefix_is_a_perfect_ndcg_at_k(self):
        """The abstract top-2 in order is the ideal at k=2: not diverged."""
        report = ScoreCalibrator(_make_domain_spec()).forward_simulate(["a", "b", "c", "d"], ["a", "b"])
        assert report.ndcg == pytest.approx(1.0, abs=1e-6)
        assert report.diverged is False

    def test_swapped_prefix_scores_below_one(self):
        abstract = ["a", "b", "c", "d"]
        concrete = ["b", "a"]
        report = ScoreCalibrator(_make_domain_spec()).forward_simulate(abstract, concrete)
        assert report.ndcg == pytest.approx(_ndcg_at_k_from_first_principles(abstract, concrete), abs=1e-6)
        assert report.ndcg < 1.0

    def test_concrete_longer_than_abstract(self):
        """k beyond the abstract list: the ideal holds only the n abstract relevances."""
        abstract = ["a", "b"]
        concrete = ["a", "b", "x", "y"]
        report = ScoreCalibrator(_make_domain_spec()).forward_simulate(abstract, concrete)
        assert report.ndcg == pytest.approx(1.0, abs=1e-6)
        assert report.diverged is False

    def test_no_overlap_is_zero(self):
        report = ScoreCalibrator(_make_domain_spec()).forward_simulate(["a", "b"], ["x", "y"])
        assert report.ndcg == 0.0
        assert report.diverged is True


@pytest.mark.unit
class TestScoreDrift:
    """Test ScoreCalibrator.detect_score_drift()."""

    def test_no_drift(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        baseline = [0.50, 0.55, 0.52, 0.53, 0.54]
        current = [0.51, 0.54, 0.53, 0.52, 0.55]
        report = calibrator.detect_score_drift(baseline, current)

        assert isinstance(report, DriftReport)
        assert report.drift_detected is False

    def test_significant_drift(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        baseline = [0.2, 0.3, 0.25, 0.22]
        current = [0.8, 0.9, 0.85, 0.88]
        report = calibrator.detect_score_drift(baseline, current)

        assert report.drift_detected is True
        assert report.delta_mean > 0.4

    def test_empty_baseline(self, monkeypatch: pytest.MonkeyPatch):
        # Rollback-path check (SCR-12): drift_insufficient_data_status_enabled off is main
        # exactly; the flag-on contract is owned by test_drift_insufficient_data_status.py.
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "drift_insufficient_data_status_enabled", False)
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.detect_score_drift([], [0.5, 0.6])

        assert report.drift_detected is True
        assert report.ks_statistic == 1.0

    def test_empty_current(self, monkeypatch: pytest.MonkeyPatch):
        # Rollback-path check (SCR-12): drift_insufficient_data_status_enabled off is main
        # exactly; the flag-on contract is owned by test_drift_insufficient_data_status.py.
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "drift_insufficient_data_status_enabled", False)
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        report = calibrator.detect_score_drift([0.5, 0.6], [])

        assert report.drift_detected is True

    def test_identical_distributions(self):
        spec = _make_domain_spec()
        calibrator = ScoreCalibrator(spec)
        scores = [0.3, 0.5, 0.7, 0.4, 0.6]
        report = calibrator.detect_score_drift(scores, list(scores))

        assert report.drift_detected is False
        assert report.ks_statistic == pytest.approx(0.0, abs=0.001)
        assert report.delta_mean == pytest.approx(0.0, abs=0.001)


@pytest.mark.unit
class TestCalibrationReport:
    """Test ScoreCalibrator.generate_calibration_report()."""

    def test_no_calibration_spec(self):
        spec = _make_domain_spec(calibration=None)
        calibrator = ScoreCalibrator(spec)
        report = calibrator.generate_calibration_report("test_domain")

        assert report["status"] == "no_calibration_spec"
        assert report["total_pairs"] == 0

    def test_with_calibration_spec(self):
        cal = CalibrationSpec(
            pairs=[
                CalibrationPair(node_a="A", node_b="B", expected_score_min=0.3, expected_score_max=0.7),
            ],
            weight_set="default",
        )
        spec = _make_domain_spec(calibration=cal)
        calibrator = ScoreCalibrator(spec)
        report = calibrator.generate_calibration_report("test_domain")

        assert report["status"] == "calibration_spec_loaded"
        assert report["total_pairs"] == 1
        assert report["weight_set"] == "default"

    def test_report_is_an_honest_spec_echo(self):
        """D6: the report echoes the declaration; its message never claims scores were verified."""
        cal = CalibrationSpec(
            pairs=[CalibrationPair(node_a="A", node_b="B", expected_score_min=0.3, expected_score_max=0.7, label="ab")],
            weight_set="default",
        )
        report = ScoreCalibrator(_make_domain_spec(calibration=cal)).generate_calibration_report("test_domain")

        assert set(report) == {"domain_id", "status", "total_pairs", "weight_set", "pairs", "message"}
        assert report["pairs"] == [{"label": "ab", "node_a": "A", "node_b": "B", "expected_range": [0.3, 0.7]}]
        message = report["message"]
        assert "No scores were computed or verified" in message
        assert "Run calibration_run to verify" not in message

    def test_empty_pairs_in_spec(self):
        cal = CalibrationSpec(pairs=[])
        spec = _make_domain_spec(calibration=cal)
        calibrator = ScoreCalibrator(spec)
        report = calibrator.generate_calibration_report("test_domain")

        assert report["status"] == "no_calibration_spec"


@pytest.mark.unit
class TestCalibrationAbstractBasisFlag:
    """D6 behind ``calibration_ndcg_abstract_basis_enabled`` (CONTRACT-21): on by default, off reproduces main."""

    @pytest.fixture
    def flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "calibration_ndcg_abstract_basis_enabled", False)

    def test_flag_defaults_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import Settings

        monkeypatch.delenv("CALIBRATION_NDCG_ABSTRACT_BASIS_ENABLED", raising=False)
        assert Settings.model_fields["calibration_ndcg_abstract_basis_enabled"].default is True
        assert Settings(l9_env="dev").calibration_ndcg_abstract_basis_enabled is True

    @pytest.mark.usefixtures("flag_off")
    def test_flag_off_zero_pairs_is_a_pass_as_on_main(self):
        report = ScoreCalibrator(_make_domain_spec()).run_calibration([], {})
        assert report.overall_pass is True
        assert report.total_pairs == 0

    @pytest.mark.usefixtures("flag_off")
    def test_flag_off_still_fails_a_failing_pair(self):
        pairs = [CalibrationPair(node_a="A", node_b="B", expected_score_min=0.5, expected_score_max=0.7)]
        report = ScoreCalibrator(_make_domain_spec()).run_calibration(pairs, {("A", "B"): 0.1})
        assert report.overall_pass is False

    @pytest.mark.usefixtures("flag_off")
    def test_flag_off_ideal_is_the_concrete_list_re_sorted_as_on_main(self):
        calibrator = ScoreCalibrator(_make_domain_spec())
        single = calibrator.forward_simulate(["a", "b", "c", "d"], ["d"])
        assert single.ndcg == pytest.approx(1.0, abs=1e-6)
        assert single.diverged is False
        # main: relevances [0, 0, 4], ideal re-sorted [4, 0, 0] -> DCG 4/log2(4) = 2 over IDCG 4.
        displaced = calibrator.forward_simulate(["a", "b", "c", "d"], ["x", "y", "a"])
        assert displaced.ndcg == pytest.approx(0.5, abs=1e-6)

    @pytest.mark.usefixtures("flag_off")
    def test_flag_off_leaves_perfect_and_reversed_rankings_unchanged(self):
        calibrator = ScoreCalibrator(_make_domain_spec())
        assert calibrator.forward_simulate(["A", "B", "C"], ["A", "B", "C"]).ndcg == pytest.approx(1.0, abs=1e-6)
        assert calibrator.forward_simulate(["A", "B", "C"], ["C", "B", "A"]).kendall_tau == pytest.approx(-1.0)

    def test_flag_on_and_off_differ_exactly_where_d6_says(self, monkeypatch: pytest.MonkeyPatch):
        from engine.config.settings import settings

        calibrator = ScoreCalibrator(_make_domain_spec())
        on = calibrator.forward_simulate(["a", "b", "c", "d"], ["d"]).ndcg
        monkeypatch.setattr(settings, "calibration_ndcg_abstract_basis_enabled", False)
        off = calibrator.forward_simulate(["a", "b", "c", "d"], ["d"]).ndcg
        assert on == pytest.approx(0.25, abs=1e-6)
        assert off == pytest.approx(1.0, abs=1e-6)
