"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [feedback]
tags: [feedback, convergence-loop, orchestrator]
owner: engine-team
status: active
--- /L9_META ---

Convergence loop orchestrator for the outcome feedback cycle.
Coordinates signal weight recalculation, pattern matching, score propagation,
and drift detection.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from engine.config.schema import DomainSpec
from engine.feedback.drift_detector import DriftDetector
from engine.feedback.pattern_matcher import ConfigurationMatcher
from engine.feedback.score_propagator import ScorePropagator
from engine.feedback.signal_weights import SignalWeightCalculator, learned_weights_applied
from engine.graph.driver import GraphDriver

logger = logging.getLogger(__name__)

# CEG-462: reported when feedback_boost_propagation_enabled is off. Nothing reads
# DimensionWeight.boost_factor, so the stage could not affect ranking.
_PROPAGATION_RETIRED: dict[str, Any] = {"propagated": False, "reason": "boost_propagation_disabled"}


class ConvergenceLoop:
    """
    Orchestrates the full feedback cycle:

    1. Outcome recorded (via handle_outcomes -- already exists)
    2. Pattern matching: find similar historical outcomes
    3. Signal weight update: recalculate if threshold met
    4. Score propagation: boost/penalize active candidate scores
    5. Drift detection: chi-squared divergence check

    This is called from handle_outcomes after the domain's outcome node
    (feedbackloop.outcome_node_label) is written to Neo4j.
    """

    def __init__(
        self,
        graph_driver: GraphDriver,
        domain_spec: DomainSpec,
    ) -> None:
        self._driver = graph_driver
        self._spec = domain_spec
        self._weight_calculator = SignalWeightCalculator(graph_driver, domain_spec)
        self._pattern_matcher = ConfigurationMatcher(graph_driver, domain_spec)
        self._score_propagator = ScorePropagator(graph_driver, domain_spec)
        self._drift_detector = DriftDetector(graph_driver, domain_spec)
        self._feedback_spec = domain_spec.feedbackloop

    async def on_outcome_recorded(
        self,
        outcome_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Called after handle_outcomes writes the domain's outcome node.
        Executes the convergence cycle.
        Returns feedback metadata for the response envelope.
        """
        if not self._feedback_spec.enabled:
            return {"feedback_loop": "disabled"}

        from engine.config.settings import settings

        if not settings.feedback_partial_results_enabled:
            return await self._run_stages_unisolated(outcome_data)

        # FEEDBACK-PARTIAL-01: the outcome node is already written when this
        # runs, so a stage failure must not erase the record of what did apply.
        # Each stage runs isolated; a failed one is reported by exception class
        # (the message can carry Cypher or driver detail) and logged in full.
        metadata: dict[str, Any] = {"feedback_loop": "enabled"}
        stages: dict[str, str] = {}

        async def _stage(name: str, run: Callable[[], Awaitable[Any]]) -> Any:
            try:
                value = await run()
            except Exception as exc:
                stages[name] = f"failed:{type(exc).__name__}"
                logger.warning(
                    "Feedback stage %s failed for domain=%s outcome_id=%s",
                    name,
                    self._spec.domain.id,
                    outcome_data.get("outcome_id"),
                    exc_info=True,
                )
                return None
            stages[name] = "ok"
            return value

        if settings.feedback_boost_propagation_enabled:
            propagation = await _stage(
                "propagation",
                lambda: self._score_propagator.propagate_outcome(
                    outcome_data,
                    boost_factor=self._feedback_spec.propagation_boost_factor,
                ),
            )
            if propagation is not None:
                metadata["propagation"] = propagation
        else:
            metadata["propagation"] = dict(_PROPAGATION_RETIRED)

        async def _recalculate() -> dict[str, Any] | None:
            if await self._weight_calculator.should_recalculate():
                weights: dict[str, Any] = await self._weight_calculator.recalculate_weights()
                return weights
            return None

        new_weights = await _stage("weights", _recalculate)
        metadata["weights_recalculated"] = stages["weights"] == "ok" and new_weights is not None
        metadata["weights_applied"] = metadata["weights_recalculated"] and learned_weights_applied()
        if new_weights is not None:
            metadata["new_weights"] = new_weights

        drift_data = await _stage(
            "drift",
            lambda: self._drift_detector.check_and_alert(
                threshold=self._feedback_spec.drift_threshold,
                window_days=self._feedback_spec.drift_window_days,
            ),
        )
        if drift_data is not None:
            metadata["drift"] = drift_data

        metadata["stages"] = stages
        metadata["status"] = "complete" if all(v == "ok" for v in stages.values()) else "partial"
        logger.info(
            "Feedback loop %s for domain=%s outcome=%s",
            metadata["status"],
            self._spec.domain.id,
            outcome_data.get("outcome"),
        )
        return metadata

    async def _run_stages_unisolated(self, outcome_data: dict[str, Any]) -> dict[str, Any]:
        """Legacy path (feedback_partial_results_enabled off): the first stage failure propagates."""
        from engine.config.settings import settings

        metadata: dict[str, Any] = {"feedback_loop": "enabled"}

        # Step 1: Propagate score adjustments (retired unless re-enabled, CEG-462)
        if settings.feedback_boost_propagation_enabled:
            metadata["propagation"] = await self._score_propagator.propagate_outcome(
                outcome_data,
                boost_factor=self._feedback_spec.propagation_boost_factor,
            )
        else:
            metadata["propagation"] = dict(_PROPAGATION_RETIRED)

        # Step 2: Check if weight recalculation needed
        if await self._weight_calculator.should_recalculate():
            weights = await self._weight_calculator.recalculate_weights()
            metadata["weights_recalculated"] = True
            metadata["weights_applied"] = learned_weights_applied()
            metadata["new_weights"] = weights
        else:
            metadata["weights_recalculated"] = False
            metadata["weights_applied"] = False

        # Step 3: Drift detection (chi-squared divergence)
        drift_data = await self._drift_detector.check_and_alert(
            threshold=self._feedback_spec.drift_threshold,
            window_days=self._feedback_spec.drift_window_days,
        )
        metadata["drift"] = drift_data

        logger.info(
            "Feedback loop completed for domain=%s outcome=%s",
            self._spec.domain.id,
            outcome_data.get("outcome"),
        )
        return metadata
