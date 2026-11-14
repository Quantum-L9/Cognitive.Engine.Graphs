"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, drift-detection, scoring, feedback]
owner: engine-team
status: active
--- /L9_META ---

SI-29: one canonical signal per drift claim.

``drift_canonical_signals_enabled`` (default off). Off, ScoreCalibrator,
hgkr_utils.check_score_drift and DriftDetector return their legacy verdicts.
On, the two score-distribution detectors delegate to
``calibration.score_distribution_drift`` and DriftDetector labels its result
``outcome_dimension_mix``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.config.settings import settings
from engine.feedback.drift_detector import DriftDetector
from engine.scoring.calibration import (
    SIGNAL_OUTCOME_DIMENSION_MIX,
    SIGNAL_SCORE_DISTRIBUTION,
    STATUS_COMPUTED,
    STATUS_INSUFFICIENT_DATA,
    DriftReport,
    ScoreCalibrator,
    score_distribution_drift,
)
from engine.scoring.hgkr_utils import DriftCheckResult, check_score_drift
from tests.unit.test_drift_detector import _minimal_spec

pytestmark = pytest.mark.unit

FLAG = "drift_canonical_signals_enabled"
BASE = [0.1, 0.2, 0.3, 0.4, 0.5]
# Same mean as BASE, very different shape: legacy mean-only check misses it.
SPREAD = [0.0, 0.0, 0.3, 0.6, 0.6]
SHIFTED = [0.4, 0.5, 0.6, 0.7, 0.8]


def _calibrator() -> ScoreCalibrator:
    return ScoreCalibrator(_minimal_spec())


class TestCanonicalSignal:
    def test_insufficient_data_is_explicit_and_not_drift(self) -> None:
        for base, cur in (([], BASE), (BASE, []), ([], [])):
            s = score_distribution_drift(base, cur)
            assert s.status == STATUS_INSUFFICIENT_DATA
            assert s.drift_detected is False
            assert (s.baseline_count, s.current_count) == (len(base), len(cur))

    def test_identical(self) -> None:
        s = score_distribution_drift(BASE, list(BASE))
        assert s.signal == SIGNAL_SCORE_DISTRIBUTION
        assert s.status == STATUS_COMPUTED
        assert s.drift_detected is False
        assert s.ks_statistic == 0.0

    def test_mean_shift(self) -> None:
        s = score_distribution_drift(BASE, SHIFTED)
        assert s.drift_detected is True
        assert s.delta_mean == pytest.approx(0.3)

    def test_shape_shift_same_mean(self) -> None:
        s = score_distribution_drift(BASE, SPREAD)
        assert s.delta_mean == pytest.approx(0.0, abs=1e-9)
        assert s.ks_statistic > s.ks_threshold
        assert s.drift_detected is True


class TestFlagOffIsLegacy:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, FLAG, False)

    def test_default_is_off(self) -> None:
        assert type(settings).model_fields[FLAG].default is False

    def test_calibrator_empty_reports_drift(self) -> None:
        assert _calibrator().detect_score_drift([], BASE) == DriftReport(
            drift_detected=True, delta_mean=0.0, ks_statistic=1.0, historical_mean=0.0, current_mean=0.0
        )

    def test_calibrator_values_match_canonical_statistics(self) -> None:
        legacy = _calibrator().detect_score_drift(BASE, SPREAD)
        s = score_distribution_drift(BASE, SPREAD)
        assert (legacy.delta_mean, legacy.ks_statistic, legacy.drift_detected) == (
            s.delta_mean,
            s.ks_statistic,
            s.drift_detected,
        )

    def test_hgkr_mean_only(self) -> None:
        r = check_score_drift(BASE, SPREAD, "job")
        assert r == DriftCheckResult(
            drift_detected=False, delta_mean=0.0, pre_scores=BASE, post_scores=SPREAD, job_name="job", threshold=0.05
        )

    @pytest.mark.asyncio
    async def test_detector_unlabelled(self) -> None:
        det = DriftDetector(AsyncMock(), _minimal_spec())
        det._query_dimension_frequency = AsyncMock(side_effect=[{"a": 1.0}, {"a": 1.0}])
        assert "signal" not in await det.compute_drift()


class TestFlagOnDelegates:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, FLAG, True)

    def test_calibrator_empty_is_not_drift(self) -> None:
        assert _calibrator().detect_score_drift([], BASE).drift_detected is False

    def test_calibrator_equals_canonical(self) -> None:
        for cur in (BASE, SPREAD, SHIFTED):
            r = _calibrator().detect_score_drift(BASE, cur)
            s = score_distribution_drift(BASE, cur)
            assert (r.drift_detected, r.delta_mean, r.ks_statistic, r.historical_mean, r.current_mean) == (
                s.drift_detected,
                s.delta_mean,
                s.ks_statistic,
                s.baseline_mean,
                s.current_mean,
            )

    def test_hgkr_agrees_with_canonical(self) -> None:
        r = check_score_drift(BASE, SPREAD, "job")
        assert r.drift_detected is True  # shape change, mean unchanged
        assert r.drift_detected == score_distribution_drift(BASE, SPREAD).drift_detected

    def test_hgkr_threshold_is_mean_threshold(self) -> None:
        assert check_score_drift(BASE, SHIFTED, "job", threshold=0.5).drift_detected is (
            score_distribution_drift(BASE, SHIFTED, mean_threshold=0.5).drift_detected
        )

    def test_hgkr_empty(self) -> None:
        assert check_score_drift([], BASE, "job").drift_detected is False

    @pytest.mark.asyncio
    async def test_detector_labelled_both_statuses(self) -> None:
        det = DriftDetector(AsyncMock(), _minimal_spec())
        det._query_dimension_frequency = AsyncMock(side_effect=[{"a": 1.0}, {"a": 0.5, "b": 0.5}])
        computed = await det.compute_drift()
        assert computed["status"] == "computed"
        assert computed["signal"] == SIGNAL_OUTCOME_DIMENSION_MIX
        det._query_dimension_frequency = AsyncMock(side_effect=[{}, {"a": 1.0}])
        insufficient = await det.check_and_alert()
        assert insufficient["status"] == "insufficient_data"
        assert insufficient["signal"] == SIGNAL_OUTCOME_DIMENSION_MIX
