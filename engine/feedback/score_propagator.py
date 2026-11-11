"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [feedback]
tags: [feedback, score-propagation, convergence]
owner: engine-team
status: active
--- /L9_META ---

Score propagator for outcome feedback.
Boosts or penalizes scores for candidates matching historical patterns.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config.schema import DomainSpec
from engine.graph.driver import GraphDriver
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)


class ScorePropagator:
    """
    When a new outcome is recorded, propagates score adjustments to
    active/future match results that share similar configurations.

    After a positive outcome, candidates matching the winning configuration
    get a boost factor applied to their scoring dimension weights.
    """

    def __init__(self, graph_driver: GraphDriver, domain_spec: DomainSpec) -> None:
        self._driver = graph_driver
        self._spec = domain_spec
        self._db = domain_spec.domain.id

    async def propagate_outcome(
        self,
        outcome_data: dict[str, Any],
        boost_factor: float = 1.15,
    ) -> dict[str, Any]:
        """
        Propagate score adjustments based on a recorded outcome.

        1. Extract the configuration from the outcome's match
        2. Find active candidates with similar configurations
        3. Update DimensionWeight boost for matching patterns
        4. Return propagation metadata (how many candidates affected)
        """
        outcome_type = outcome_data.get("outcome", "")
        candidate_id = outcome_data.get("candidate_id", "")

        if outcome_type not in ("success", "failure"):
            return {"propagated": False, "reason": "partial_outcome_no_action"}
        factor = boost_factor if outcome_type == "success" else 1.0 / boost_factor

        from engine.config.settings import settings

        outcome_id = outcome_data.get("outcome_id")
        if settings.outcome_idempotency_enabled and settings.propagation_single_application and outcome_id:
            claim = await self._apply_claimed_boost(str(outcome_id), candidate_id, factor)
            if claim is None:
                return {"propagated": False, "reason": "outcome_not_found", "outcome_id": outcome_id}
            applied, affected = claim
            if not applied:
                return {"propagated": False, "reason": "already_applied", "outcome_id": outcome_id}
        else:
            affected = await self._apply_positive_boost(candidate_id, factor)

        return {
            "propagated": True,
            "outcome_type": outcome_type,
            "candidates_affected": affected,
            "factor_applied": boost_factor if outcome_type == "success" else 1.0 / boost_factor,
        }

    async def _apply_positive_boost(
        self,
        candidate_id: str,
        factor: float,
    ) -> int:
        """
        Apply boost factor to DimensionWeight nodes linked to the candidate's pattern.

        Uses MERGE to create or update DimensionWeight boost nodes.

        RU-028 / CEG-COR-117: while ``settings.propagation_single_application``
        is on (default) the candidate's outcomes are collapsed to one existence
        row (``WITH count(o) AS outcomes WHERE outcomes > 0``) before the
        DimensionWeight MATCH, so SET runs once per DimensionWeight per event
        and ``affected`` == |DimensionWeight|; the product is clamped to the
        spec's ``signal_weights`` [min_weight, max_weight]. Off: the legacy
        query, whose rows multiply (factor^k for k prior outcomes, unbounded).
        """
        from engine.config.settings import settings

        outcome_label = sanitize_label(self._spec.feedbackloop.outcome_node_label)
        parameters: dict[str, Any] = {
            "candidate_id": candidate_id,
            "tenant": self._db,
            "factor": factor,
        }

        if settings.propagation_single_application:
            sw_spec = self._spec.feedbackloop.signal_weights
            parameters["min_weight"] = sw_spec.min_weight
            parameters["max_weight"] = sw_spec.max_weight
            cypher = f"""
        MATCH (o:{outcome_label} {{candidate_id: $candidate_id, tenant: $tenant}})
        WITH count(o) AS outcomes
        WHERE outcomes > 0
        MATCH (dw:DimensionWeight {{tenant: $tenant}})
        WITH dw, coalesce(dw.boost_factor, 1.0) * $factor AS boosted
        SET dw.boost_factor = CASE
                WHEN boosted > $max_weight THEN $max_weight
                WHEN boosted < $min_weight THEN $min_weight
                ELSE boosted
            END,
            dw.last_propagation_at = datetime()
        RETURN count(dw) AS affected
        """
        else:
            # Find similar candidates and boost their dimension weights
            cypher = f"""
        MATCH (o:{outcome_label} {{candidate_id: $candidate_id, tenant: $tenant}})
        WITH o
        MATCH (dw:DimensionWeight {{tenant: $tenant}})
        SET dw.boost_factor = coalesce(dw.boost_factor, 1.0) * $factor,
            dw.last_propagation_at = datetime()
        RETURN count(dw) AS affected
        """
        result = await self._driver.execute_query(
            cypher,
            parameters=parameters,
            database=self._db,
        )

        affected = result[0]["affected"] if result else 0
        logger.info(
            "Propagated outcome for candidate=%s factor=%.3f affected=%d",
            candidate_id,
            factor,
            affected,
        )
        return affected

    async def _apply_claimed_boost(
        self,
        outcome_id: str,
        candidate_id: str,
        factor: float,
    ) -> tuple[bool, int] | None:
        """Apply the boost at most once per outcome_id (FEEDBACK-IDEMP-01).

        One statement, one transaction: the outcome node is write-locked by a
        temporary property before ``propagated_at`` is read (Neo4j runs read
        committed, and a plain read-then-SET loses concurrent updates), then
        stamped. Only the transaction that found it unstamped multiplies the
        DimensionWeights, each locked by its own SET, with the single-application
        clamp. A failed transaction rolls the stamp back, so a retry can apply.

        Returns ``(applied, affected)``, or None when no outcome node carries
        ``outcome_id`` for this tenant.
        """
        outcome_label = sanitize_label(self._spec.feedbackloop.outcome_node_label)
        sw_spec = self._spec.feedbackloop.signal_weights
        cypher = f"""
        MATCH (claim:{outcome_label} {{outcome_id: $outcome_id, tenant: $tenant}})
        SET claim._propagation_lock = true
        REMOVE claim._propagation_lock
        WITH claim, claim.propagated_at IS NULL AS first_application
        SET claim.propagated_at = coalesce(claim.propagated_at, datetime())
        WITH first_application
        OPTIONAL MATCH (o:{outcome_label} {{candidate_id: $candidate_id, tenant: $tenant}})
        WHERE first_application
        WITH first_application, count(o) AS outcomes
        OPTIONAL MATCH (dw:DimensionWeight {{tenant: $tenant}})
        WHERE first_application AND outcomes > 0
        FOREACH (_ IN CASE WHEN dw IS NULL THEN [] ELSE [1] END |
            SET dw._propagation_lock = true
            REMOVE dw._propagation_lock
            SET dw.boost_factor = CASE
                    WHEN coalesce(dw.boost_factor, 1.0) * $factor > $max_weight THEN $max_weight
                    WHEN coalesce(dw.boost_factor, 1.0) * $factor < $min_weight THEN $min_weight
                    ELSE coalesce(dw.boost_factor, 1.0) * $factor
                END,
                dw.last_propagation_at = datetime()
        )
        RETURN first_application AS applied, count(dw) AS affected
        """
        result = await self._driver.execute_query(
            cypher,
            parameters={
                "outcome_id": outcome_id,
                "candidate_id": candidate_id,
                "tenant": self._db,
                "factor": factor,
                "min_weight": sw_spec.min_weight,
                "max_weight": sw_spec.max_weight,
            },
            database=self._db,
        )
        if not result:
            logger.warning("No outcome node for outcome_id=%s; propagation skipped", outcome_id)
            return None
        applied = bool(result[0]["applied"])
        affected = int(result[0]["affected"]) if applied else 0
        logger.info(
            "Propagation claim outcome_id=%s applied=%s factor=%.3f affected=%d",
            outcome_id,
            applied,
            factor,
            affected,
        )
        return applied, affected
