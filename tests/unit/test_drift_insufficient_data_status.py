"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, drift, calibration]
owner: engine-team
status: active
--- /L9_META ---

SCR-12 (audit L9): insufficient data is reported as insufficient, never as a verdict.

``ScoreCalibrator.detect_score_drift`` (``DriftReport``) and
``hgkr_utils.check_score_drift`` (``DriftCheckResult``) both answer "did the
score distribution shift?". Given a sample with no finite value they disagreed:
with ``drift_canonical_signals_enabled`` off, the calibrator reported drift
(``ks_statistic`` 1.0) for an empty sample and computed a NaN mean for an
all-NaN one, while ``check_score_drift`` reported no drift. With the canonical
flag on, both said "no drift" and dropped ``ScoreDriftSignal.status``, so the
caller could not tell "measured, stable" from "nothing to measure".

With ``drift_insufficient_data_status_enabled`` on (default), both results carry
``status``: ``insufficient_data`` (``drift_detected`` False, statistics 0.0)
when either sample has no finite value, else ``computed``. This holds in both
states of ``drift_canonical_signals_enabled``. Samples with finite data keep each
detector's existing verdict. Off: every pre-existing field is main's, and
``status`` is ``None``.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.settings import Settings, settings
from engine.scoring.calibration import STATUS_COMPUTED, STATUS_INSUFFICIENT_DATA, ScoreCalibrator
from engine.scoring.hgkr_utils import check_score_drift

pytestmark = pytest.mark.unit

_NAN = float("nan")
_INF = float("inf")

INSUFFICIENT_CASES = [
    pytest.param([], [0.5, 0.6], id="empty-baseline"),
    pytest.param([0.5, 0.6], [], id="empty-current"),
    pytest.param([], [], id="both-empty"),
    pytest.param([_NAN, _NAN], [0.5, 0.6], id="all-nan-baseline"),
    pytest.param([0.5, 0.6], [_NAN, _INF, -_INF], id="all-non-finite-current"),
]
DRIFT = ([0.2, 0.3, 0.25, 0.22], [0.8, 0.9, 0.85, 0.88])
STABLE = ([0.3, 0.5, 0.7, 0.4, 0.6], [0.3, 0.5, 0.7, 0.4, 0.6])


def _calibrator() -> ScoreCalibrator:
    spec = MagicMock()
    spec.domain.id = "test_domain"
    return ScoreCalibrator(spec)


def _both(baseline: list[float], current: list[float]) -> tuple[Any, Any]:
    return (
        _calibrator().detect_score_drift(list(baseline), list(current)),
        check_score_drift(list(baseline), list(current), job_name="louvain"),
    )


@pytest.fixture(params=[False, True], ids=["canonical-off", "canonical-on"])
def canonical(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(settings, "drift_canonical_signals_enabled", request.param)
    monkeypatch.setattr(settings, "drift_insufficient_data_status_enabled", True)
    return bool(request.param)


@pytest.fixture
def status_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "drift_insufficient_data_status_enabled", False)


def test_flag_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIFT_INSUFFICIENT_DATA_STATUS_ENABLED", raising=False)
    assert Settings.model_fields["drift_insufficient_data_status_enabled"].default is True
    assert Settings(l9_env="dev").drift_insufficient_data_status_enabled is True


# ── Flag on: insufficient data is insufficient, in both canonical states ─────


@pytest.mark.parametrize(("baseline", "current"), INSUFFICIENT_CASES)
def test_insufficient_data_is_reported_as_insufficient_by_both(
    canonical: bool, baseline: list[float], current: list[float]
) -> None:
    report, result = _both(baseline, current)
    for value in (report, result):
        assert value.status == STATUS_INSUFFICIENT_DATA
        assert value.drift_detected is False
        assert value.delta_mean == 0.0
    assert report.ks_statistic == 0.0
    assert report.historical_mean == 0.0
    assert report.current_mean == 0.0
    # No NaN leaks out of an insufficient report.
    assert not any(math.isnan(v) for v in (report.delta_mean, report.ks_statistic, result.delta_mean))


def test_normal_drift_is_computed_and_detected_by_both(canonical: bool) -> None:
    report, result = _both(*DRIFT)
    assert report.status == result.status == STATUS_COMPUTED
    assert report.drift_detected is True
    assert result.drift_detected is True
    assert report.delta_mean == pytest.approx(result.delta_mean, abs=1e-6)


def test_no_drift_is_computed_and_not_detected_by_both(canonical: bool) -> None:
    report, result = _both(*STABLE)
    assert report.status == result.status == STATUS_COMPUTED
    assert report.drift_detected is False
    assert result.drift_detected is False
    assert report.ks_statistic == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize(("baseline", "current"), [*INSUFFICIENT_CASES, DRIFT, STABLE])
def test_both_detectors_agree_on_the_verdict_and_status(
    canonical: bool, baseline: list[float], current: list[float]
) -> None:
    report, result = _both(baseline, current)
    assert report.status == result.status
    assert report.drift_detected == result.drift_detected


# ── Flag off: main exactly ───────────────────────────────────────────────────


@pytest.mark.usefixtures("status_off")
def test_flag_off_canonical_off_keeps_mains_disagreement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "drift_canonical_signals_enabled", False)
    report, result = _both([], [0.5, 0.6])
    assert (report.drift_detected, report.ks_statistic, report.status) == (True, 1.0, None)
    assert (result.drift_detected, result.delta_mean, result.status) == (False, 0.0, None)

    nan_report = _calibrator().detect_score_drift([_NAN, _NAN], [0.5, 0.6])
    assert math.isnan(nan_report.historical_mean)
    assert nan_report.status is None


@pytest.mark.usefixtures("status_off")
def test_flag_off_canonical_on_drops_the_status_as_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "drift_canonical_signals_enabled", True)
    report, result = _both([], [0.5, 0.6])
    assert (report.drift_detected, report.ks_statistic, report.status) == (False, 0.0, None)
    assert (result.drift_detected, result.status) == (False, None)


@pytest.mark.usefixtures("status_off")
@pytest.mark.parametrize("canonical_flag", [False, True])
def test_flag_off_sufficient_data_values_are_unchanged(monkeypatch: pytest.MonkeyPatch, canonical_flag: bool) -> None:
    monkeypatch.setattr(settings, "drift_canonical_signals_enabled", canonical_flag)
    off_report, off_result = _both(*DRIFT)
    monkeypatch.setattr(settings, "drift_insufficient_data_status_enabled", True)
    on_report, on_result = _both(*DRIFT)
    for field in ("drift_detected", "delta_mean", "ks_statistic", "historical_mean", "current_mean"):
        assert getattr(off_report, field) == getattr(on_report, field)
    for field in ("drift_detected", "delta_mean"):
        assert getattr(off_result, field) == getattr(on_result, field)
    assert off_report.status is None
    assert off_result.status is None
