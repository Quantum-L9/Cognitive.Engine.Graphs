"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [causal]
tags: [causal, counterfactual, scenario-generation]
owner: engine-team
status: active
--- /L9_META ---

Counterfactual scenario generation for negative outcomes.
Follows retrieval-then-reasoning: extract causal neighborhood,
compare with positive outcomes, generate intervention hypotheses.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from engine.config.schema import CounterfactualSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.models.outcomes import decode_outcome_map
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

_INTERVENTION_ADD_DIMENSION = "add_scoring_dimension"
_INTERVENTION_CHANGE_WEIGHT = "change_weight"
_INTERVENTION_ADD_GATE = "add_gate"

# SI-26: what a scenario's number measures and what it does not claim. A
# scenario compares one negative outcome with a pool of positive outcomes; it
# never runs the intervention, so the counterfactual outcome is untested.
_SUPPORT_BASIS = "share_of_compared_positive_outcomes"
_CLAIM_CO_OCCURRENCE = "co_occurrence"
_OUTCOME_UNTESTED = "untested"


class CounterfactualGenerator:
    """Generates CounterfactualScenario nodes for negative outcomes.

    Strategy (inspired by ReasoningLM's subgraph reasoning):
    1. RETRIEVE: Extract the outcome's match fingerprint
    2. COMPARE: Find historical positive outcomes with similar configurations
    3. GENERATE: For each key difference, create a CounterfactualScenario node
    4. SCORE: Assign confidence based on how many winners share the factor
       (SI-26, ``counterfactual_support_semantics_enabled``: that number is a
       co-occurrence ``support``, not a confidence, and the outcome is untested)
    """

    def __init__(
        self,
        counterfactual_spec: CounterfactualSpec,
        graph_driver: GraphDriver,
        domain_id: str,
        outcome_label: str,
    ) -> None:
        self._spec = counterfactual_spec
        self._graph_driver = graph_driver
        self._db = domain_id
        # RU-075 / CEG-COR-122: the domain's feedbackloop.outcome_node_label
        # (passed by handle_outcomes); the queries hardcoded :TransactionOutcome.
        self._outcome_label = sanitize_label(outcome_label)

    def _label(self) -> str:
        """Outcome label used in the fingerprint queries (legacy literal when the flag is off)."""
        if not settings.counterfactual_dedupe_and_spec_label:
            return "TransactionOutcome"
        return sanitize_label(self._outcome_label)

    async def generate_for_outcome(
        self,
        outcome_node_id: str,
    ) -> list[dict[str, Any]]:
        """Generate counterfactual scenarios for a negative outcome.

        Args:
            outcome_node_id: The outcome_id of the negative outcome node.

        Returns:
            List of created CounterfactualScenario dicts.
        """
        fingerprint = await self._get_outcome_fingerprint(outcome_node_id)
        if not fingerprint:
            logger.info("No fingerprint found for outcome %s", outcome_node_id)
            return []

        positive_fingerprints = await self._find_similar_positives(fingerprint)
        if not positive_fingerprints:
            logger.info("No similar positive outcomes for comparison")
            return []

        diff = (
            self._diff_fingerprints_support
            if settings.counterfactual_support_semantics_enabled
            else self._diff_fingerprints
        )
        scenarios = diff(
            actual_fingerprint=fingerprint,
            positive_fingerprints=positive_fingerprints,
            outcome_node_id=outcome_node_id,
        )

        if scenarios:
            await self._store_scenarios(scenarios)
            logger.info(
                "Generated %d counterfactual scenarios for outcome %s",
                len(scenarios),
                outcome_node_id,
            )

        return scenarios

    async def _get_outcome_fingerprint(self, outcome_node_id: str) -> dict[str, Any] | None:
        """Retrieve the match fingerprint from the domain's outcome node."""
        label = self._label()
        cypher = f"""
        MATCH (o:{label} {{outcome_id: $outcome_id}})
        RETURN o.active_dimensions AS active_dimensions,
               o.dimension_weights AS dimension_weights,
               o.gates_passed AS gates_passed,
               o.match_direction AS match_direction,
               o.candidate_count AS candidate_count,
               o.outcome AS outcome
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"outcome_id": outcome_node_id},
            database=self._db,
        )
        if not results:
            return None
        row = results[0]
        return {
            "active_dimensions": row.get("active_dimensions") or [],
            "dimension_weights": decode_outcome_map(row.get("dimension_weights")),
            "gates_passed": row.get("gates_passed") or [],
            "match_direction": row.get("match_direction"),
            "candidate_count": row.get("candidate_count", 0),
            "outcome": row.get("outcome"),
        }

    async def _find_similar_positives(self, fingerprint: dict[str, Any]) -> list[dict[str, Any]]:
        """Find positive outcomes with overlapping dimensions."""
        pool_size = self._spec.comparison_pool_size
        match_direction = fingerprint.get("match_direction")

        label = self._label()
        cypher = f"""
        MATCH (o:{label})
        WHERE o.outcome = 'success'
              AND o.active_dimensions IS NOT NULL
              AND o.match_direction = $match_direction
        RETURN o.active_dimensions AS active_dimensions,
               o.dimension_weights AS dimension_weights,
               o.gates_passed AS gates_passed,
               o.outcome_id AS outcome_id
        ORDER BY o.created_at DESC
        LIMIT $pool_size
        """
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"match_direction": match_direction, "pool_size": pool_size},
            database=self._db,
        )
        return [
            {
                "active_dimensions": r.get("active_dimensions") or [],
                "dimension_weights": decode_outcome_map(r.get("dimension_weights")),
                "gates_passed": r.get("gates_passed") or [],
                "outcome_id": r.get("outcome_id"),
            }
            for r in results
        ]

    def _diff_fingerprints(
        self,
        actual_fingerprint: dict[str, Any],
        positive_fingerprints: list[dict[str, Any]],
        outcome_node_id: str,
    ) -> list[dict[str, Any]]:
        """Identify key differences between losing and winning configs."""
        actual_dims = set(actual_fingerprint.get("active_dimensions") or [])
        max_scenarios = self._spec.max_scenarios_per_outcome
        min_confidence = self._spec.min_confidence
        total_positives = len(positive_fingerprints)

        if total_positives == 0:
            return []

        # Count how many positive outcomes have each dimension
        dim_counts: dict[str, int] = {}
        for pf in positive_fingerprints:
            for dim in pf.get("active_dimensions") or []:
                dim_counts[dim] = dim_counts.get(dim, 0) + 1

        scenarios: list[dict[str, Any]] = []
        # RU-075: one scenario per (intervention_type, key_difference); the
        # weight loop below otherwise emitted the same change_weight once per
        # qualifying positive, consuming max_scenarios slots.
        dedupe = settings.counterfactual_dedupe_and_spec_label
        seen: set[tuple[str, str]] = set()

        # Dimensions present in positives but missing from actual
        missing_dims = {dim: count for dim, count in dim_counts.items() if dim not in actual_dims}
        for dim, count in sorted(missing_dims.items(), key=lambda x: -x[1]):
            confidence = count / total_positives
            if confidence < min_confidence:
                continue
            scenarios.append(
                {
                    "scenario_id": f"cf_{uuid.uuid4().hex[:12]}",
                    "actual_outcome": outcome_node_id,
                    "counterfactual_outcome": "success",
                    "intervention_type": _INTERVENTION_ADD_DIMENSION,
                    "confidence": round(confidence, 4),
                    "key_difference": dim,
                }
            )
            if len(scenarios) >= max_scenarios:
                break

        # Weight differences for shared dimensions
        if len(scenarios) < max_scenarios:
            actual_weights = actual_fingerprint.get("dimension_weights") or {}
            for pf in positive_fingerprints:
                pw = pf.get("dimension_weights") or {}
                for dim in actual_dims:
                    if dim in pw and dim in actual_weights:
                        actual_w = actual_weights[dim]
                        positive_w = pw[dim]
                        if isinstance(actual_w, (int, float)) and isinstance(positive_w, (int, float)):
                            if positive_w > actual_w * 1.5:
                                confidence = dim_counts.get(dim, 0) / total_positives
                                key = (_INTERVENTION_CHANGE_WEIGHT, f"{dim}_weight_increase")
                                if confidence >= min_confidence and not (dedupe and key in seen):
                                    seen.add(key)
                                    scenarios.append(
                                        {
                                            "scenario_id": f"cf_{uuid.uuid4().hex[:12]}",
                                            "actual_outcome": outcome_node_id,
                                            "counterfactual_outcome": "success",
                                            "intervention_type": _INTERVENTION_CHANGE_WEIGHT,
                                            "confidence": round(confidence, 4),
                                            "key_difference": f"{dim}_weight_increase",
                                        }
                                    )
                    if len(scenarios) >= max_scenarios:
                        break
                if len(scenarios) >= max_scenarios:
                    break

        return scenarios[:max_scenarios]

    def _diff_fingerprints_support(
        self,
        actual_fingerprint: dict[str, Any],
        positive_fingerprints: list[dict[str, Any]],
        outcome_node_id: str,
    ) -> list[dict[str, Any]]:
        """SI-26: the differences, labelled as co-occurrence support, not causal confidence.

        ``support`` is the share of the compared positive outcomes that carry
        the difference: for ``add_scoring_dimension`` the positives with the
        dimension active, for ``change_weight`` the positives whose weight for
        the dimension exceeds 1.5x the actual one (the legacy value counted
        positives with the dimension active, whatever its weight). It is an
        association between a configuration factor and success within the
        compared pool; it does not estimate the effect of the intervention,
        which is never run, so ``counterfactual_outcome`` is ``untested``.
        ``spec.min_confidence`` is applied as the minimum support.
        """
        actual_dims = set(actual_fingerprint.get("active_dimensions") or [])
        max_scenarios = self._spec.max_scenarios_per_outcome
        min_support = self._spec.min_confidence
        total_positives = len(positive_fingerprints)
        if total_positives == 0:
            return []

        def scenario(intervention_type: str, key_difference: str, count: int) -> dict[str, Any]:
            return {
                "scenario_id": f"cf_{uuid.uuid4().hex[:12]}",
                "actual_outcome": outcome_node_id,
                "counterfactual_outcome": _OUTCOME_UNTESTED,
                "intervention_type": intervention_type,
                "support": round(count / total_positives, 4),
                "support_count": count,
                "compared_positives": total_positives,
                "support_basis": _SUPPORT_BASIS,
                "claim": _CLAIM_CO_OCCURRENCE,
                "key_difference": key_difference,
            }

        candidates = [
            (_INTERVENTION_ADD_DIMENSION, dim, count)
            for dim, count in _by_support(_missing_dimension_counts(actual_dims, positive_fingerprints))
        ]
        candidates += [
            (_INTERVENTION_CHANGE_WEIGHT, f"{dim}_weight_increase", count)
            for dim, count in _by_support(
                _heavier_weight_counts(actual_dims, actual_fingerprint, positive_fingerprints)
            )
        ]

        scenarios: list[dict[str, Any]] = []
        for intervention_type, key_difference, count in candidates:
            if len(scenarios) >= max_scenarios:
                break
            if count / total_positives >= min_support:
                scenarios.append(scenario(intervention_type, key_difference, count))
        return scenarios

    async def _store_scenarios(self, scenarios: list[dict[str, Any]]) -> None:
        """Persist CounterfactualScenario nodes in Neo4j."""
        if settings.counterfactual_dedupe_and_spec_label:
            await self._merge_scenarios(scenarios)
            return
        if settings.counterfactual_support_semantics_enabled:
            await self._store_support_scenarios(scenarios)
            return
        cypher = """
        UNWIND $scenarios AS s
        CREATE (cs:CounterfactualScenario {
            scenario_id: s.scenario_id,
            actual_outcome: s.actual_outcome,
            counterfactual_outcome: s.counterfactual_outcome,
            intervention_type: s.intervention_type,
            confidence: s.confidence,
            key_difference: s.key_difference,
            domain_id: $domain_id,
            created_at: datetime()
        })
        """
        await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"scenarios": scenarios, "domain_id": self._db},
            database=self._db,
        )

    async def _merge_scenarios(self, scenarios: list[dict[str, Any]]) -> None:
        """C16 (``counterfactual_dedupe_and_spec_label``): idempotent scenario write.

        A retry for an outcome whose scenarios are already stored adds no
        duplicate node; each returned scenario carries the id actually stored.
        """
        fields = _SUPPORT_FIELDS if settings.counterfactual_support_semantics_enabled else _LEGACY_FIELDS
        rows = await self._graph_driver.execute_query(
            cypher=_merge_scenarios_cypher(fields),
            parameters={"scenarios": scenarios, "domain_id": self._db},
            database=self._db,
        )
        stored = {
            row["proposed_id"]: row["scenario_id"]
            for row in rows or []
            if isinstance(row, dict) and row.get("proposed_id") and row.get("scenario_id")
        }
        for scenario in scenarios:
            scenario["scenario_id"] = stored.get(scenario["scenario_id"], scenario["scenario_id"])

    async def _store_support_scenarios(self, scenarios: list[dict[str, Any]]) -> None:
        """SI-26: persist scenarios with their support fields and no confidence property."""
        cypher = """
        UNWIND $scenarios AS s
        CREATE (cs:CounterfactualScenario {
            scenario_id: s.scenario_id,
            actual_outcome: s.actual_outcome,
            counterfactual_outcome: s.counterfactual_outcome,
            intervention_type: s.intervention_type,
            support: s.support,
            support_count: s.support_count,
            compared_positives: s.compared_positives,
            support_basis: s.support_basis,
            claim: s.claim,
            key_difference: s.key_difference,
            domain_id: $domain_id,
            created_at: datetime()
        })
        """
        await self._graph_driver.execute_query(
            cypher=cypher,
            parameters={"scenarios": scenarios, "domain_id": self._db},
            database=self._db,
        )


_LEGACY_FIELDS = ("counterfactual_outcome", "confidence")
_SUPPORT_FIELDS = (
    "counterfactual_outcome",
    "support",
    "support_count",
    "compared_positives",
    "support_basis",
    "claim",
)


def _merge_scenarios_cypher(fields: tuple[str, ...]) -> str:
    """MERGE one CounterfactualScenario per semantic key; first write wins.

    The key is ``(actual_outcome, intervention_type, key_difference,
    counterfactual_outcome, domain_id)``: the RU-075 intervention key per
    outcome, split by the claim mode so a legacy ``success`` scenario and an
    ``untested`` support scenario never merge into one node. Field names are
    module constants, never input.
    """
    on_create = ", ".join(f"{sanitize_label(field)}: s.{sanitize_label(field)}" for field in ("scenario_id", *fields))
    return (
        "UNWIND $scenarios AS s\n"
        "MERGE (cs:CounterfactualScenario {actual_outcome: s.actual_outcome, "
        "intervention_type: s.intervention_type, key_difference: s.key_difference, "
        "counterfactual_outcome: s.counterfactual_outcome, domain_id: $domain_id})\n"
        f"ON CREATE SET cs += {{{on_create}, created_at: datetime()}}\n"
        "RETURN s.scenario_id AS proposed_id, cs.scenario_id AS scenario_id"
    )


def _by_support(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Dimensions by descending support count, ties by name."""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _missing_dimension_counts(actual_dims: set[str], positive_fingerprints: list[dict[str, Any]]) -> dict[str, int]:
    """SI-26: positives per dimension active there but not in the actual run."""
    counts: dict[str, int] = {}
    for pf in positive_fingerprints:
        for dim in set(pf.get("active_dimensions") or []) - actual_dims:
            counts[dim] = counts.get(dim, 0) + 1
    return counts


def _heavier_weight_counts(
    actual_dims: set[str],
    actual_fingerprint: dict[str, Any],
    positive_fingerprints: list[dict[str, Any]],
) -> dict[str, int]:
    """SI-26: positives per active dimension weighted over 1.5x the actual weight."""
    actual_weights = actual_fingerprint.get("dimension_weights") or {}
    counts: dict[str, int] = {}
    for pf in positive_fingerprints:
        positive_weights = pf.get("dimension_weights") or {}
        for dim in actual_dims:
            actual_w = actual_weights.get(dim)
            positive_w = positive_weights.get(dim)
            if (
                isinstance(actual_w, (int, float))
                and isinstance(positive_w, (int, float))
                and positive_w > actual_w * 1.5
            ):
                counts[dim] = counts.get(dim, 0) + 1
    return counts
