"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [feedback]
tags: [feedback, signal-weights, scoring]
owner: engine-team
status: active
--- /L9_META ---

Signal weight calculator for outcome-based weight learning.
Computes dimension weights from historical outcome correlations,
with 95% confidence intervals and dampening toward neutral.
"""

from __future__ import annotations

import logging
import math
from typing import Any
from uuid import uuid4

from engine.config.outcome_vocabulary import outcome_node_label
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

_Z_95 = 1.96

# CEG-462: a recalculation that is not applied is recorded as a candidate. The
# prior weight is read, never written; no DimensionWeight node is created.
# DimensionWeightChange is append-only: nothing in the engine updates or deletes it.
_CANDIDATE_CHANGE_CYPHER = """
        OPTIONAL MATCH (dw:DimensionWeight {dimension: $dimension, tenant: $tenant})
        WITH head(collect(dw.weight)) AS prior_weight
        CREATE (c:DimensionWeightChange {
            tenant: $tenant, dimension: $dimension, recalculation_id: $recalculation_id,
            prior_weight: prior_weight, new_weight: $weight, applied: false,
            confidence: $confidence, ci_width: $ci_width, lift: $lift, sample_size: $sample_size,
            outcome_count: $outcome_count, win_count: $win_count, method: $method,
            recorded_at: datetime()
        })
        RETURN c.dimension AS dimension
        """

# Latest recalculation: the newer of the last applied weight and the last change record.
_LAST_RECALC_WITH_RECORDS = """OPTIONAL MATCH (dw:DimensionWeight {tenant: $tenant})
        WITH max(dw.updated_at) AS last_applied
        OPTIONAL MATCH (c:DimensionWeightChange {tenant: $tenant})
        WITH last_applied, max(c.recorded_at) AS last_recorded
        WITH CASE
            WHEN last_recorded IS NULL THEN last_applied
            WHEN last_applied IS NOT NULL AND last_applied > last_recorded THEN last_applied
            ELSE last_recorded
        END AS last_recalc"""


def learned_weights_applied() -> bool:
    """Whether a recalculation writes ``DimensionWeight.weight`` (CEG-462).

    True on the legacy path (no change record) and under approved automatic
    recalculation; False when the recalculation is recorded as an unapplied
    candidate, which leaves the weights ScoringAssembler reads unchanged.
    """
    return not settings.learned_weight_change_record_enabled or settings.learned_weight_auto_apply_enabled


def _recalculation_method() -> str:
    """Name of the calculation that produced a recorded weight, so a record can be reproduced."""
    ci = "agresti_coull" if settings.signal_weight_proportion_ci else "legacy_lift_ci"
    return f"outcome_lift/{ci}/v1"


def _lift_ci_width(wins: int, n: int, base_win_rate: float) -> float:
    """95% CI width of the dimension win rate, expressed on the lift scale.

    RU-027 / CEG-COR-116: the interval belongs to the proportion
    ``p = wins / n``, not to the lift. It uses the Agresti-Coull form of
    ``1.96 * sqrt(p (1 - p) / n)`` (``z^2 / 2`` pseudo-wins and pseudo-losses),
    so it is non-zero at ``p`` = 0 or 1 and shrinks as ``n`` grows for any lift,
    including lift >= 1; dividing by ``base_win_rate`` maps it onto the lift scale.
    """
    z2 = _Z_95 * _Z_95
    n_adj = n + z2
    p_adj = (wins + z2 / 2.0) / n_adj
    proportion_width = _Z_95 * math.sqrt(p_adj * (1.0 - p_adj) / n_adj)
    return proportion_width / base_win_rate


class SignalWeightCalculator:
    """
    Calculates signal/dimension weights based on outcome correlation.

    For each scoring dimension type, computes:
      w_i = P(positive_outcome | dimension_i_high) / P(positive_outcome) x frequency_factor

    Stores weights as properties on DimensionWeight configuration nodes in Neo4j.
    """

    def __init__(self, graph_driver: GraphDriver, domain_spec: DomainSpec) -> None:
        self._driver = graph_driver
        self._spec = domain_spec
        self._weight_spec = domain_spec.feedbackloop.signal_weights
        self._db = domain_spec.domain.id

    async def recalculate_weights(self) -> dict[str, float]:
        """
        Query the domain's outcome nodes (feedbackloop.outcome_node_label), compute win rates per dimension config,
        update DimensionWeight nodes in Neo4j.

        Returns mapping of dimension name to computed weight.
        """
        outcome_label = sanitize_label(outcome_node_label(self._spec, "signal weight recalculation"))

        # Step 1: Get total outcome counts
        totals_cypher = f"""
        MATCH (o:{outcome_label})
        WHERE o.tenant = $tenant
        RETURN count(o) AS total,
               sum(CASE WHEN o.outcome = 'success' THEN 1 ELSE 0 END) AS wins
        """
        totals = await self._driver.execute_query(
            totals_cypher,
            parameters={"tenant": self._db},
            database=self._db,
        )

        total_outcomes = totals[0]["total"] if totals else 0
        total_wins = totals[0]["wins"] if totals else 0

        if total_outcomes == 0:
            return self._baseline_weights()

        base_win_rate = total_wins / total_outcomes if total_outcomes > 0 else 0.0

        # Step 2: For each scoring dimension, compute correlation with wins
        weights: dict[str, float] = {}
        weight_details: dict[str, dict[str, float]] = {}
        for dim in self._spec.scoring.dimensions:
            detail = await self._compute_dimension_weight(
                dim.name,
                base_win_rate,
                total_outcomes,
                outcome_label,
            )
            weights[dim.name] = detail["weight"]
            weight_details[dim.name] = detail

        # Step 3: Store weights in Neo4j (with confidence metadata)
        await self._store_weights(weight_details, outcome_count=total_outcomes, win_count=total_wins)

        logger.info(
            "Recalculated %d dimension weights for domain=%s",
            len(weights),
            self._db,
        )
        return weights

    async def _compute_dimension_weight(
        self,
        dimension_name: str,
        base_win_rate: float,
        total_outcomes: int,
        outcome_label: str,
    ) -> dict[str, float]:
        """Compute weight for a single dimension based on outcome correlation.

        Returns a dict with keys: weight, confidence, ci_width, lift, sample_size.
        """
        # Validated again here so this helper is safe on its own, not only via its callers (C-009).
        outcome_label = sanitize_label(outcome_label)
        safe_dim = sanitize_label(dimension_name)

        # Query outcomes where this dimension scored above median
        dim_cypher = f"""
        MATCH (o:{outcome_label})
        WHERE o.tenant = $tenant AND o.{safe_dim}_score IS NOT NULL
        WITH count(o) AS dim_total,
             sum(CASE WHEN o.outcome = 'success' THEN 1 ELSE 0 END) AS dim_wins
        RETURN dim_total, dim_wins
        """
        result = await self._driver.execute_query(
            dim_cypher,
            parameters={"tenant": self._db},
            database=self._db,
        )

        dim_total = result[0]["dim_total"] if result else 0
        dim_wins = result[0]["dim_wins"] if result else 0

        if dim_total == 0 or math.isclose(base_win_rate, 0.0, abs_tol=1e-9):
            return {
                "weight": self._weight_spec.baseline_weight,
                "confidence": 0.0,
                "ci_width": 1.0,
                "lift": 1.0,
                "sample_size": dim_total,
            }

        dim_win_rate = dim_wins / dim_total
        lift = dim_win_rate / base_win_rate

        # Frequency adjustment: penalize rare signals
        frequency_factor = 1.0
        if self._weight_spec.frequency_adjustment and total_outcomes > 0:
            frequency_ratio = dim_total / total_outcomes
            # Sigmoid-like scaling: penalizes dimensions present in <10% of outcomes
            frequency_factor = min(1.0, frequency_ratio * 10.0)

        # Confidence interval calculation (95% CI)
        sample_size = max(dim_total, 1)
        if settings.signal_weight_proportion_ci:
            ci_width = _lift_ci_width(dim_wins, sample_size, base_win_rate)
        else:
            ci_width = 1.96 * math.sqrt(lift * (1.0 - min(lift, 1.0)) / sample_size)
        confidence = 1.0 - min(ci_width, 1.0)

        # Dampening: blend toward 1.0 when confidence is low
        if self._weight_spec.confidence_dampening:
            dampened_weight = 1.0 + (lift - 1.0) * confidence * frequency_factor
        else:
            dampened_weight = lift * frequency_factor

        # Clamp to configured bounds
        clamped = max(
            self._weight_spec.min_weight,
            min(self._weight_spec.max_weight, dampened_weight),
        )

        return {
            "weight": clamped,
            "confidence": round(confidence, 6),
            "ci_width": round(ci_width, 6),
            "lift": round(lift, 6),
            "sample_size": dim_total,
        }

    async def _store_weights(
        self,
        weight_details: dict[str, dict[str, float]],
        *,
        outcome_count: int = 0,
        win_count: int = 0,
    ) -> None:
        """Store computed weights as DimensionWeight nodes in Neo4j with confidence metadata.

        CEG-462: with ``learned_weight_change_record_enabled`` each dimension of a
        recalculation appends one ``DimensionWeightChange`` node. The weight is
        written only when ``learned_weights_applied()``; otherwise the record is an
        unapplied candidate and DimensionWeight is not touched. Off: the legacy
        overwrite with no history.
        """
        if not settings.learned_weight_change_record_enabled:
            await self._overwrite_weights(weight_details)
            return
        apply = settings.learned_weight_auto_apply_enabled
        recalculation_id = str(uuid4())
        method = _recalculation_method()
        for dim_name, detail in weight_details.items():
            parameters: dict[str, Any] = {
                "dimension": dim_name,
                "tenant": self._db,
                "weight": detail["weight"],
                "confidence": detail["confidence"],
                "ci_width": detail["ci_width"],
                "lift": detail["lift"],
                "sample_size": detail["sample_size"],
                "outcome_count": outcome_count,
                "win_count": win_count,
                "recalculation_id": recalculation_id,
                "method": method,
            }
            cypher = self._applied_change_cypher(dim_name) if apply else _CANDIDATE_CHANGE_CYPHER
            await self._driver.execute_query(cypher, parameters=parameters, database=self._db)
        logger.info(
            "Recorded %d dimension weight changes for domain=%s recalculation_id=%s applied=%s",
            len(weight_details),
            self._db,
            recalculation_id,
            apply,
        )

    @staticmethod
    def _applied_change_cypher(dim_name: str) -> str:
        """Write the weight and its change record in one statement.

        The DimensionWeight is write-locked before its prior weight is read, so
        two concurrent recalculations cannot both record the same prior value.
        """
        safe_name = sanitize_label(dim_name)
        return f"""
        MERGE (dw:DimensionWeight {{dimension: $dimension, tenant: $tenant}})
        SET dw._weight_lock = true
        REMOVE dw._weight_lock
        WITH dw, dw.weight AS prior_weight
        SET dw.weight = $weight,
            dw.confidence = $confidence,
            dw.ci_width = $ci_width,
            dw.updated_at = datetime(),
            dw.{safe_name}_weight = $weight
        CREATE (:DimensionWeightChange {{
            tenant: $tenant, dimension: $dimension, recalculation_id: $recalculation_id,
            prior_weight: prior_weight, new_weight: $weight, applied: true,
            confidence: $confidence, ci_width: $ci_width, lift: $lift, sample_size: $sample_size,
            outcome_count: $outcome_count, win_count: $win_count, method: $method,
            recorded_at: dw.updated_at
        }})
        RETURN dw.dimension AS dimension
        """

    async def _overwrite_weights(self, weight_details: dict[str, dict[str, float]]) -> None:
        """Legacy write (learned_weight_change_record_enabled off): overwrite, no history."""
        for dim_name, detail in weight_details.items():
            safe_name = sanitize_label(dim_name)
            cypher = f"""
            MERGE (dw:DimensionWeight {{dimension: $dimension, tenant: $tenant}})
            SET dw.weight = $weight,
                dw.confidence = $confidence,
                dw.ci_width = $ci_width,
                dw.updated_at = datetime(),
                dw.{safe_name}_weight = $weight
            RETURN dw.dimension AS dimension
            """
            await self._driver.execute_query(
                cypher,
                parameters={
                    "dimension": dim_name,
                    "tenant": self._db,
                    "weight": detail["weight"],
                    "confidence": detail["confidence"],
                    "ci_width": detail["ci_width"],
                },
                database=self._db,
            )

    async def should_recalculate(self) -> bool:
        """Check if recalculation threshold met (outcome count since last recalc)."""
        outcome_label = sanitize_label(outcome_node_label(self._spec, "signal weight recalculation check"))

        # CEG-462: an unapplied recalculation does not move dw.updated_at, so the
        # latest change record also marks a recalculation; without it every
        # outcome past the threshold would recalculate (and record) again.
        last_recalc = (
            _LAST_RECALC_WITH_RECORDS
            if settings.learned_weight_change_record_enabled
            else "OPTIONAL MATCH (dw:DimensionWeight {tenant: $tenant})\n        WITH max(dw.updated_at) AS last_recalc"
        )
        cypher = f"""
        {last_recalc}
        MATCH (o:{outcome_label})
        WHERE o.tenant = $tenant
          AND (last_recalc IS NULL OR o.created_at > last_recalc)
        RETURN count(o) AS new_outcomes
        """
        result = await self._driver.execute_query(
            cypher,
            parameters={"tenant": self._db},
            database=self._db,
        )

        new_outcomes = result[0]["new_outcomes"] if result else 0
        return new_outcomes >= self._weight_spec.min_outcomes_for_recalculation

    async def get_current_weights(self) -> dict[str, float]:
        """Read current DimensionWeight nodes from graph."""
        cypher = """
        MATCH (dw:DimensionWeight {tenant: $tenant})
        RETURN dw.dimension AS dimension, dw.weight AS weight
        """
        results = await self._driver.execute_query(
            cypher,
            parameters={"tenant": self._db},
            database=self._db,
        )

        weights: dict[str, float] = {}
        for record in results:
            weights[record["dimension"]] = record["weight"]

        if not weights:
            return self._baseline_weights()

        return weights

    def _baseline_weights(self) -> dict[str, float]:
        """Return baseline weights for all scoring dimensions."""
        return {dim.name: self._weight_spec.baseline_weight for dim in self._spec.scoring.dimensions}
