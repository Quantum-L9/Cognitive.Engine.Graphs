"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [causal]
tags: [causal, attribution, multi-touch]
owner: engine-team
status: active
--- /L9_META ---

Multi-touch attribution calculator for outcome causation.
Traces backward through causal edges to identify and weight contributing factors.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

# Valid attribution model names
VALID_MODELS = frozenset({"first_touch", "last_touch", "linear", "position_based"})


def _temporal_decay_factor(age_days: float, halflife_days: float) -> float:
    """Exponential recency decay in [0, 1]: exp(-age / halflife).

    Matches the repo's temporal-decay convention (engine/scoring/assembler.py
    ``_compile_temporalproximity``). age 0 (or missing timestamp) -> 1.0 (no
    decay); older links decay toward 0. ``halflife_days`` is assumed positive
    (sourced from settings.decay_transaction_halflife, default 180.0).
    """
    if halflife_days <= 0:
        return 1.0
    return math.exp(-max(age_days, 0.0) / halflife_days)


class AttributionCalculator:
    """
    Multi-touch attribution for outcome causation.

    Given an outcome (TransactionOutcome), traces backward through
    causal edges to identify and weight contributing factors.
    Supports: first_touch, last_touch, linear, position_based attribution models.
    """

    def __init__(self, graph_driver: GraphDriver, domain_spec: DomainSpec) -> None:
        self._driver = graph_driver
        self._spec = domain_spec
        self._db = domain_spec.domain.id

    async def compute_attribution(
        self,
        outcome_node_id: str,
        model: str = "linear",
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """
        Trace causal chain from outcome backward, assign attribution weights.
        Returns {touchpoint_id: weight} mapping and metadata.
        """
        if model not in VALID_MODELS:
            msg = f"Invalid attribution model: {model!r}. Must be one of {sorted(VALID_MODELS)}"
            raise ValueError(msg)

        depth = max_depth or self._spec.causal.chain_depth_limit
        outcome_label = sanitize_label(self._spec.feedbackloop.outcome_node_label)

        # Build edge pattern from causal spec
        edge_types = [sanitize_label(e.edge_type) for e in self._spec.causal.causal_edges]
        if edge_types:
            edge_pattern = "|".join(edge_types)
            rel_pattern = f"[:{edge_pattern}*1..{depth}]"
        else:
            rel_pattern = f"[*1..{depth}]"

        # Trace backward from outcome to find contributing touchpoints. Per-edge
        # ages (in days) are computed in Cypher for the temporal-decay refinement;
        # a missing created_at coalesces to now -> age 0 -> no decay (NULL-safe).
        cypher = (
            f"MATCH (outcome:{outcome_label} {{outcome_id: $outcome_id}})\n"
            f"MATCH path = (touchpoint)-{rel_pattern}->(outcome)\n"
            f"RETURN touchpoint.entity_id AS touchpoint_id,\n"
            f"       length(path) AS distance,\n"
            f"       [r IN relationships(path) | r.confidence] AS confidences,\n"
            f"       [r IN relationships(path) | duration.inDays(coalesce(r.created_at, datetime()), datetime()).days] AS ages_days\n"
            f"ORDER BY distance ASC"
        )

        results = await self._driver.execute_query(
            cypher,
            parameters={"outcome_id": outcome_node_id},
            database=self._db,
        )

        if not results:
            return {"touchpoints": {}, "model": model, "chain_depth": 0}

        touchpoints = self._assign_weights(results, model)

        response: dict[str, Any] = {
            "touchpoints": touchpoints,
            "model": model,
            "chain_depth": max((r["distance"] for r in results), default=0),
            "total_touchpoints": len(touchpoints),
        }

        # Temporal-decay refinement (feature-flagged, per-domain). Down-weights
        # touchpoints reached through older causal links and renormalizes so the
        # attribution weights still sum to 1.0. Flag off -> weights unchanged.
        if self._spec.causal.temporal_decay_enabled:
            halflife = settings.decay_transaction_halflife
            ages_by_touchpoint: dict[str, float] = {}
            for r in results:
                tp_id = r["touchpoint_id"]
                if not tp_id:
                    continue
                ages = [a for a in (r.get("ages_days") or []) if a is not None]
                # A touchpoint's chain is only as fresh as its oldest link.
                if not ages:
                    ages_by_touchpoint[tp_id] = 0.0
                else:
                    try:
                        ages_by_touchpoint[tp_id] = float(max(ages))
                    except (TypeError, ValueError):
                        ages_by_touchpoint[tp_id] = 0.0
            response["touchpoints"] = self._apply_temporal_decay(touchpoints, ages_by_touchpoint, halflife)
            response["temporal_decay"] = {"enabled": True, "halflife_days": halflife}

        return response

    @staticmethod
    def _assign_weights(
        results: list[dict[str, Any]],
        model: str,
    ) -> dict[str, float]:
        """Assign attribution weights based on the selected model."""
        touchpoint_ids = [r["touchpoint_id"] for r in results if r["touchpoint_id"]]
        n = len(touchpoint_ids)

        if n == 0:
            return {}

        weights: dict[str, float] = {}

        if model == "first_touch":
            # All credit to the first touchpoint
            weights[touchpoint_ids[0]] = 1.0
            for tp_id in touchpoint_ids[1:]:
                weights[tp_id] = 0.0

        elif model == "last_touch":
            # All credit to the last touchpoint
            for tp_id in touchpoint_ids[:-1]:
                weights[tp_id] = 0.0
            weights[touchpoint_ids[-1]] = 1.0

        elif model == "linear":
            # Equal credit to all touchpoints
            equal_weight = 1.0 / n
            for tp_id in touchpoint_ids:
                weights[tp_id] = round(equal_weight, 6)

        elif model == "position_based":
            # 40% first, 40% last, 20% distributed among middle
            if n == 1:
                weights[touchpoint_ids[0]] = 1.0
            elif n == 2:
                weights[touchpoint_ids[0]] = 0.5
                weights[touchpoint_ids[1]] = 0.5
            else:
                weights[touchpoint_ids[0]] = 0.4
                weights[touchpoint_ids[-1]] = 0.4
                middle_weight = 0.2 / (n - 2)
                for tp_id in touchpoint_ids[1:-1]:
                    weights[tp_id] = round(middle_weight, 6)

        return weights

    @staticmethod
    def _apply_temporal_decay(
        weights: dict[str, float],
        ages_days: dict[str, float],
        halflife_days: float,
    ) -> dict[str, float]:
        """Down-weight touchpoints by causal-link age, then renormalize to sum 1.0.

        Each base weight is multiplied by ``exp(-age / halflife)``; the result is
        renormalized so the attribution invariant (weights sum to 1.0, bounded in
        [0, 1]) is preserved. A touchpoint with no recorded age (0.0) is not
        decayed. If every decayed weight is zero (degenerate), the base weights
        are returned unchanged.
        """
        decayed = {
            tp_id: base * _temporal_decay_factor(ages_days.get(tp_id, 0.0), halflife_days)
            for tp_id, base in weights.items()
        }
        total = sum(decayed.values())
        if total <= 0:
            return weights
        return {tp_id: round(value / total, 6) for tp_id, value in decayed.items()}
