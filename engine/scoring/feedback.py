"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [scoring, feedback, outcome, wave2]
owner: engine-team
status: active
--- /L9_META ---

Outcome feedback loop for weight tuning proposals.

Reads outcome data (success/failure/partial; RU-006) from Neo4j, computes
per-dimension discriminability, and proposes weight adjustments. Does NOT
auto-apply — returns proposals for human-in-the-loop approval.

seL4 analog: the feedback mechanism closes the refinement loop — the
abstract target (calibration spec) constrains where the concrete weights
are allowed to move.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# Maximum weight nudge per feedback cycle
WEIGHT_NUDGE = 0.02
# Minimum outcomes required for meaningful analysis
MIN_OUTCOMES = 10


def outcome_classes() -> tuple[str, str]:
    """The (positive, negative) outcome values this analyzer partitions on.

    RU-006 / CEG-COR-015: the canonical outcome record (handle_outcomes, and the
    sync outcome type under ``outcome_schema_unified``) stores
    success|failure|partial, so success is the positive class and failure the
    negative one; partial is neither. Off: the legacy positive|negative labels.
    """
    from engine.config.settings import settings

    if settings.outcome_schema_unified:
        return "success", "failure"
    return "positive", "negative"


class OutcomeFeedback:
    """Compute weight adjustment proposals from outcome feedback data.

    This class processes outcome records and produces per-dimension
    discriminability analysis and weight adjustment proposals. The
    proposals are advisory — they must be explicitly applied via the
    apply_weight_proposal admin subaction.
    """

    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = outcomes

    def compute_feedback(self) -> dict[str, Any]:
        """Analyze outcomes and propose weight adjustments.

        Returns dict with:
            - sample_count: number of outcomes analyzed
            - sufficient_data: whether at least MIN_OUTCOMES outcomes were given
              and at least one dimension has scores from both classes (RU-025),
              each holding at least ``feedback_min_samples_per_class`` scores
              while ``feedback_per_class_minimum_enabled`` is on (SCR-13)
            - dimension_analysis: per-dimension discriminability
            - proposed_weights: dict of dimension -> proposed weight delta
        """
        if len(self.outcomes) < MIN_OUTCOMES:
            return {
                "sample_count": len(self.outcomes),
                "sufficient_data": False,
                "dimension_analysis": {},
                "proposed_weights": {},
                "message": f"Insufficient outcomes ({len(self.outcomes)} < {MIN_OUTCOMES}). Collect more data.",
            }

        positive_class, negative_class = outcome_classes()
        from engine.config.settings import settings

        # SCR-13: one scored outcome per class is not evidence of discriminability.
        min_per_class = (
            int(settings.feedback_min_samples_per_class) if settings.feedback_per_class_minimum_enabled else 1
        )

        # Partition scores by outcome type
        positive_dims: dict[str, list[float]] = defaultdict(list)
        negative_dims: dict[str, list[float]] = defaultdict(list)

        for outcome in self.outcomes:
            if not isinstance(outcome, dict):
                continue  # type: ignore[unreachable]
            outcome_type = outcome.get("outcome")
            dim_scores = outcome.get("dimension_scores", {})
            if not isinstance(dim_scores, dict):
                continue

            for dim, score in dim_scores.items():
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    continue
                number = float(score)
                if not math.isfinite(number):
                    continue
                if outcome_type == positive_class:
                    positive_dims[dim].append(number)
                elif outcome_type == negative_class:
                    negative_dims[dim].append(number)

        all_dims = set(positive_dims.keys()) | set(negative_dims.keys())
        dimension_analysis: dict[str, dict[str, Any]] = {}
        proposed_weights: dict[str, float] = {}

        for dim in sorted(all_dims):
            pos_scores = positive_dims.get(dim, [])
            neg_scores = negative_dims.get(dim, [])
            # RU-025 / CEG-COR-032: discriminability needs both classes. A
            # missing class is no evidence, not a mean of 0.0, so the dimension
            # is recorded with its counts and gets no proposal. SCR-13: nor is a
            # class below the per-class minimum.
            if len(pos_scores) < min_per_class or len(neg_scores) < min_per_class:
                dimension_analysis[dim] = {
                    "positive_mean": round(statistics.mean(pos_scores), 6) if pos_scores else None,
                    "negative_mean": round(statistics.mean(neg_scores), 6) if neg_scores else None,
                    "discriminability": None,
                    "positive_count": len(pos_scores),
                    "negative_count": len(neg_scores),
                    "proposed_nudge": None,
                }
                continue
            pos_mean = statistics.mean(pos_scores)
            neg_mean = statistics.mean(neg_scores)
            discriminability = pos_mean - neg_mean

            # Nudge proportional to discriminability, capped at WEIGHT_NUDGE
            nudge = max(-WEIGHT_NUDGE, min(WEIGHT_NUDGE, WEIGHT_NUDGE * discriminability))
            proposed_weights[dim] = round(nudge, 6)

            dimension_analysis[dim] = {
                "positive_mean": round(pos_mean, 6),
                "negative_mean": round(neg_mean, 6),
                "discriminability": round(discriminability, 6),
                "positive_count": len(pos_scores),
                "negative_count": len(neg_scores),
                "proposed_nudge": round(nudge, 6),
            }

        result: dict[str, Any] = {
            "sample_count": len(self.outcomes),
            "sufficient_data": bool(proposed_weights),
            "dimension_analysis": dimension_analysis,
            "proposed_weights": proposed_weights,
        }
        if min_per_class > 1:
            result["min_samples_per_class"] = min_per_class
        if not proposed_weights and min_per_class > 1:
            result["message"] = (
                f"No dimension has at least {min_per_class} scored outcomes of each class "
                f"({positive_class!r} and {negative_class!r}); nothing to compare. "
                "Collect outcomes with dimension_scores."
            )
        elif not proposed_weights:
            result["message"] = (
                f"No dimension has scored outcomes of both classes ({positive_class!r} and "
                f"{negative_class!r}); nothing to compare. Collect outcomes with dimension_scores."
            )
        return result

    @staticmethod
    def apply_weights(
        current_weights: dict[str, float],
        proposed_deltas: dict[str, float],
    ) -> dict[str, float]:
        """Apply proposed weight deltas to current weights.

        Clamps each resulting weight to [0.0, 1.0].

        Args:
            current_weights: Current weight set (dimension -> weight).
            proposed_deltas: Proposed nudges (dimension -> delta).

        Returns:
            New weight dict with deltas applied and clamped.
        """
        result: dict[str, float] = {}
        for dim, current in current_weights.items():
            delta = proposed_deltas.get(dim, 0.0)
            result[dim] = round(max(0.0, min(1.0, current + delta)), 6)
        return result
