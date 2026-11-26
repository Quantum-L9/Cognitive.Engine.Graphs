"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [scoring, calibration, forward-simulation, wave2]
owner: engine-team
status: active
--- /L9_META ---

Score calibration framework — seL4-inspired forward simulation.

Library checks of a concrete scoring outcome against the abstract
calibration spec defined by the domain author: pass/fail per calibration pair
(``run_calibration``, given the caller's actual scores), NDCG ranking quality
and Kendall-tau correlation (``forward_simulate``), and distribution drift
(simplified KS test).

This is a MANUAL verification tool — it does NOT auto-adjust weights. The
``calibration_run`` admin subaction does not call these checks: it returns
``generate_calibration_report``, an echo of the declared spec (D6).
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Any

from engine.config.schema import CalibrationPair, DomainSpec

logger = logging.getLogger(__name__)


def _abstract_basis() -> bool:
    """D6 flag ``calibration_ndcg_abstract_basis_enabled``: zero pairs fail and NDCG uses the abstract ideal."""
    from engine.config.settings import settings

    return settings.calibration_ndcg_abstract_basis_enabled


def _dcg(relevances: list[float]) -> float:
    """Compute Discounted Cumulative Gain for a relevance list."""
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


@dataclass
class CalibrationResult:
    """Result for a single calibration pair check."""

    pair_label: str
    node_a: str
    node_b: str
    actual_score: float | None
    expected_min: float
    expected_max: float
    passed: bool
    diff: float


@dataclass
class ForwardSimReport:
    """Result of forward simulation: abstract vs concrete ranking comparison."""

    ndcg: float
    kendall_tau: float
    diverged: bool
    misranked_pairs: list[tuple[str, str]]


@dataclass
class DriftReport:
    """Result of score drift detection between baseline and current scores."""

    drift_detected: bool
    delta_mean: float
    ks_statistic: float
    historical_mean: float
    current_mean: float
    # SCR-12: ``computed`` or ``insufficient_data`` under
    # ``drift_insufficient_data_status_enabled``; None with the flag off.
    status: str | None = None


@dataclass
class CalibrationReport:
    """Full calibration report for a domain."""

    domain_id: str
    total_pairs: int
    passed: int
    failed: int
    results: list[CalibrationResult]
    overall_pass: bool


DRIFT_KS_THRESHOLD = 0.2
DRIFT_MEAN_THRESHOLD = 0.05

# SI-29: the claims the engine's drift detectors answer. One signal per claim.
SIGNAL_SCORE_DISTRIBUTION = "score_distribution"
SIGNAL_OUTCOME_DIMENSION_MIX = "outcome_dimension_mix"
STATUS_COMPUTED = "computed"
STATUS_INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class ScoreDriftSignal:
    """SI-29: the canonical answer to "did the score distribution shift between two samples?".

    ``status`` is ``insufficient_data`` when either sample is empty: no
    comparison was made, so ``drift_detected`` is ``False`` and the
    statistics are 0.0 — an absent sample is not evidence of drift.
    """

    signal: str
    status: str
    drift_detected: bool
    delta_mean: float
    ks_statistic: float
    baseline_mean: float
    current_mean: float
    baseline_count: int
    current_count: int
    mean_threshold: float
    ks_threshold: float


def _finite_samples(values: list[float]) -> list[float]:
    """Drop nulls, bools and non-finite numbers. They are not a distribution."""
    finite: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            finite.append(number)
    return finite


def score_distribution_drift(
    baseline_scores: list[float],
    current_scores: list[float],
    mean_threshold: float = DRIFT_MEAN_THRESHOLD,
    ks_threshold: float = DRIFT_KS_THRESHOLD,
) -> ScoreDriftSignal:
    """SI-29: canonical score-distribution drift (two-sample KS statistic and mean shift).

    Drift is detected when ``|mean(current) - mean(baseline)| > mean_threshold``
    or the two-sample Kolmogorov-Smirnov statistic ``max |CDF_b - CDF_c|``
    exceeds ``ks_threshold``. It measures score values only; which scoring
    dimensions outcomes used is a different claim
    (``engine.feedback.drift_detector.DriftDetector``).
    """
    baseline_scores = _finite_samples(baseline_scores)
    current_scores = _finite_samples(current_scores)
    if not baseline_scores or not current_scores:
        return ScoreDriftSignal(
            signal=SIGNAL_SCORE_DISTRIBUTION,
            status=STATUS_INSUFFICIENT_DATA,
            drift_detected=False,
            delta_mean=0.0,
            ks_statistic=0.0,
            baseline_mean=0.0,
            current_mean=0.0,
            baseline_count=len(baseline_scores),
            current_count=len(current_scores),
            mean_threshold=mean_threshold,
            ks_threshold=ks_threshold,
        )
    base_mean = statistics.mean(baseline_scores)
    curr_mean = statistics.mean(current_scores)
    delta_mean = curr_mean - base_mean
    n_b, n_c = len(baseline_scores), len(current_scores)
    ks_stat = 0.0
    for p in sorted(set(baseline_scores) | set(current_scores)):
        cdf_b = sum(1 for v in baseline_scores if v <= p) / n_b
        cdf_c = sum(1 for v in current_scores if v <= p) / n_c
        ks_stat = max(ks_stat, abs(cdf_b - cdf_c))
    return ScoreDriftSignal(
        signal=SIGNAL_SCORE_DISTRIBUTION,
        status=STATUS_COMPUTED,
        drift_detected=abs(delta_mean) > mean_threshold or ks_stat > ks_threshold,
        delta_mean=round(delta_mean, 6),
        ks_statistic=round(ks_stat, 6),
        baseline_mean=round(base_mean, 6),
        current_mean=round(curr_mean, 6),
        baseline_count=n_b,
        current_count=n_c,
        mean_threshold=mean_threshold,
        ks_threshold=ks_threshold,
    )


class ScoreCalibrator:
    """Calibrates scoring against domain-spec calibration pairs.

    seL4 analog: forward simulation — the abstract spec defines expected
    score ranges; this class verifies the implementation satisfies them.
    """

    def __init__(self, domain_spec: DomainSpec) -> None:
        self.domain_spec = domain_spec

    def run_calibration(
        self,
        calibration_pairs: list[CalibrationPair],
        actual_scores: dict[tuple[str, str], float],
    ) -> CalibrationReport:
        """Run calibration against actual scores.

        Args:
            calibration_pairs: Expected score ranges from domain spec.
            actual_scores: Map of (node_a, node_b) -> actual score from engine.

        Returns:
            CalibrationReport with pass/fail per pair and overall status.
            D6: zero pairs is not a pass — with nothing checked there is no
            evidence the scores satisfy the spec, so ``overall_pass`` is False
            (flag ``calibration_ndcg_abstract_basis_enabled``; off: a pass).
        """
        results: list[CalibrationResult] = []
        for pair in calibration_pairs:
            key = (pair.node_a, pair.node_b)
            actual = actual_scores.get(key)
            if actual is not None:
                passed = pair.expected_score_min <= actual <= pair.expected_score_max
                diff = 0.0
                if actual < pair.expected_score_min:
                    diff = pair.expected_score_min - actual
                elif actual > pair.expected_score_max:
                    diff = actual - pair.expected_score_max
            else:
                passed = False
                diff = -1.0  # sentinel: score not available

            results.append(
                CalibrationResult(
                    pair_label=pair.label or f"{pair.node_a}->{pair.node_b}",
                    node_a=pair.node_a,
                    node_b=pair.node_b,
                    actual_score=actual,
                    expected_min=pair.expected_score_min,
                    expected_max=pair.expected_score_max,
                    passed=passed,
                    diff=round(diff, 6),
                )
            )

        passed_count = sum(1 for r in results if r.passed)
        domain_id = self.domain_spec.domain.id

        return CalibrationReport(
            domain_id=domain_id,
            total_pairs=len(results),
            passed=passed_count,
            failed=len(results) - passed_count,
            results=results,
            overall_pass=passed_count == len(results) and (bool(results) or not _abstract_basis()),
        )

    def forward_simulate(
        self,
        abstract_ranking: list[str],
        concrete_ranking: list[str],
    ) -> ForwardSimReport:
        """Compare abstract intent vs concrete scoring ranking.

        Computes NDCG and Kendall-tau to quantify ranking divergence.

        D6: NDCG is NDCG@k with k = ``len(concrete_ranking)`` and the ideal
        DCG taken from the abstract ranking itself (its top k relevances),
        not from re-sorting the concrete list's own relevances. Re-sorting
        made any ranking that omitted or displaced the abstract top items
        its own ideal, so ``[a,b,c,d]`` vs ``[d]`` scored 1.0 and was not
        reported diverged. Flag ``calibration_ndcg_abstract_basis_enabled``;
        off restores the re-sorted basis.

        Args:
            abstract_ranking: Ideal ordering by domain author (most preferred first).
            concrete_ranking: Actual ordering produced by scoring engine.
        """
        if not abstract_ranking or not concrete_ranking:
            return ForwardSimReport(ndcg=0.0, kendall_tau=0.0, diverged=True, misranked_pairs=[])

        n = len(abstract_ranking)
        abstract_pos: dict[str, int] = {cid: i for i, cid in enumerate(abstract_ranking)}

        # NDCG: relevance based on position in abstract ranking
        # float() of int arithmetic (list length minus list index): cannot raise.
        relevances_actual = [float(n - abstract_pos[cid]) if cid in abstract_pos else 0.0 for cid in concrete_ranking]
        if _abstract_basis():
            ideal_relevances = [float(n - i) for i in range(min(n, len(concrete_ranking)))]
        else:
            ideal_relevances = sorted(relevances_actual, reverse=True)
        idcg = _dcg(ideal_relevances)
        ndcg = min(1.0, _dcg(relevances_actual) / idcg) if idcg > 0 else 0.0

        # Kendall-tau on common elements
        common = [cid for cid in concrete_ranking if cid in abstract_pos]
        concordant = discordant = 0
        for i in range(len(common)):
            for j in range(i + 1, len(common)):
                if abstract_pos[common[i]] < abstract_pos[common[j]]:
                    concordant += 1
                elif abstract_pos[common[i]] > abstract_pos[common[j]]:
                    discordant += 1
        total_pairs = concordant + discordant
        tau = (concordant - discordant) / total_pairs if total_pairs > 0 else 0.0

        # Misranked pairs
        concrete_pos: dict[str, int] = {cid: i for i, cid in enumerate(concrete_ranking)}
        misranked: list[tuple[str, str]] = []
        for i, a in enumerate(abstract_ranking):
            for b in abstract_ranking[i + 1 :]:
                if a in concrete_pos and b in concrete_pos and concrete_pos[a] > concrete_pos[b]:
                    misranked.append((a, b))

        drift_threshold = 0.05
        return ForwardSimReport(
            ndcg=round(ndcg, 6),
            kendall_tau=round(tau, 6),
            diverged=ndcg < (1.0 - drift_threshold),
            misranked_pairs=misranked[:20],
        )

    def detect_score_drift(
        self,
        baseline_scores: list[float],
        current_scores: list[float],
    ) -> DriftReport:
        """Detect scoring distribution drift via simplified KS test.

        Args:
            baseline_scores: Historical final scores.
            current_scores: Current period final scores.

        SI-29: with ``drift_canonical_signals_enabled`` on, this delegates to
        :func:`score_distribution_drift`; an empty sample is then reported as
        no drift (``ks_statistic`` 0.0) rather than as drift. Off: legacy.

        SCR-12: with ``drift_insufficient_data_status_enabled`` on, a sample
        with no finite value is ``status`` ``insufficient_data`` (no drift,
        statistics 0.0) in both SI-29 states, as in ``check_score_drift``, and
        any other result is ``computed``. Off: ``status`` is None.
        """
        from engine.config.settings import settings

        report_status = settings.drift_insufficient_data_status_enabled
        if report_status and not (_finite_samples(baseline_scores) and _finite_samples(current_scores)):
            # SCR-12: no finite value on one side is no evidence either way.
            return DriftReport(
                drift_detected=False,
                delta_mean=0.0,
                ks_statistic=0.0,
                historical_mean=0.0,
                current_mean=0.0,
                status=STATUS_INSUFFICIENT_DATA,
            )
        computed = STATUS_COMPUTED if report_status else None

        if settings.drift_canonical_signals_enabled:
            signal = score_distribution_drift(baseline_scores, current_scores)
            return DriftReport(
                drift_detected=signal.drift_detected,
                delta_mean=signal.delta_mean,
                ks_statistic=signal.ks_statistic,
                historical_mean=signal.baseline_mean,
                current_mean=signal.current_mean,
                status=signal.status if report_status else None,
            )

        if not baseline_scores or not current_scores:
            return DriftReport(
                drift_detected=True,
                delta_mean=0.0,
                ks_statistic=1.0,
                historical_mean=0.0,
                current_mean=0.0,
            )

        hist_mean = statistics.mean(baseline_scores)
        curr_mean = statistics.mean(current_scores)
        delta_mean = curr_mean - hist_mean

        # Simplified KS statistic: max |CDF_h - CDF_c|
        all_points = sorted(set(baseline_scores + current_scores))
        n_h, n_c = len(baseline_scores), len(current_scores)
        hist_sorted = sorted(baseline_scores)
        curr_sorted = sorted(current_scores)

        ks_stat = 0.0
        for p in all_points:
            cdf_h = sum(1 for v in hist_sorted if v <= p) / n_h
            cdf_c = sum(1 for v in curr_sorted if v <= p) / n_c
            ks_stat = max(ks_stat, abs(cdf_h - cdf_c))

        drift_threshold = 0.05
        drift_detected = abs(delta_mean) > drift_threshold or ks_stat > DRIFT_KS_THRESHOLD

        return DriftReport(
            drift_detected=drift_detected,
            delta_mean=round(delta_mean, 6),
            ks_statistic=round(ks_stat, 6),
            historical_mean=round(hist_mean, 6),
            current_mean=round(curr_mean, 6),
            status=computed,
        )

    def generate_calibration_report(self, domain_id: str) -> dict[str, Any]:
        """Echo the domain spec's calibration declaration for the admin API.

        D6: this is a spec echo. It lists the declared pairs and expected
        ranges; it computes no score and verifies nothing, and the message
        says so. The status key ``calibration_spec_loaded`` and the payload
        keys are unchanged.
        """
        cal_spec = self.domain_spec.calibration
        if not cal_spec or not cal_spec.pairs:
            return {
                "domain_id": domain_id,
                "status": "no_calibration_spec",
                "total_pairs": 0,
                "message": "No calibration pairs defined in domain spec",
            }

        return {
            "domain_id": domain_id,
            "status": "calibration_spec_loaded",
            "total_pairs": len(cal_spec.pairs),
            "weight_set": cal_spec.weight_set,
            "pairs": [
                {
                    "label": p.label or f"{p.node_a}->{p.node_b}",
                    "node_a": p.node_a,
                    "node_b": p.node_b,
                    "expected_range": [p.expected_score_min, p.expected_score_max],
                }
                for p in cal_spec.pairs
            ],
            "message": (
                "Calibration spec loaded: declared pairs and expected ranges only. No scores were computed or verified."
            ),
        }
