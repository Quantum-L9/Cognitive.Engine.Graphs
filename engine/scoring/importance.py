"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [hoprag, importance, visit-count, scoring]
owner: engine-team
status: active
--- /L9_META ---

Visit-count importance scoring dimension.

Implements the IMP metric from HopRAG (ACL 2025, §3.3.3):

    IMP(v_i, C_count) = C_count[v_i] / sum(C_count)

where C_count is a dictionary mapping vertex IDs to visit counts
accumulated during multi-hop BFS traversal. Vertices visited more
frequently from different reasoning paths are considered more important.

Wiring status (SCR-11): dormant. No engine or chassis module imports this one,
there is no ``ComputationType`` for it (``ScoringAssembler`` cannot dispatch to
it), and no domain pack can declare it; HopRAG is parked (CEG-KGE-PARK,
``docs/PARKED_SUBSYSTEMS.md``). ``engine.traversal.multihop.MultiHopTraverser``
returns visit counts in memory and writes no ``visit_count`` property, so
``compile_importance_cypher`` reads a property no engine code writes; the
in-memory ``ImportanceScorer`` takes whatever counts its caller passes.
``engine.scoring.helpfulness`` does not import it either. Pinned as dormant by
``tests/unit/test_score_numeric_lifecycle.py``.

With ``importance_strict_inputs_enabled`` (default on) ``compute`` raises for a
vertex that is not in ``visit_counts``, as documented, and the Cypher expression
scores ``default_when_null`` when ``$total_visits`` is null or not positive
instead of dividing by it. Off: an unknown vertex scores 0.0 and the division
is unguarded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from engine.utils.security import cypher_number, sanitize_label

logger = logging.getLogger(__name__)


def _strict_inputs() -> bool:
    """SCR-11: reject an unknown vertex and guard the Cypher division (``importance_strict_inputs_enabled``)."""
    from engine.config.settings import settings

    return bool(settings.importance_strict_inputs_enabled)


@dataclass(frozen=True)
class ImportanceResult:
    """Result of an importance computation.

    Attributes:
        score: Normalized importance in [0.0, 1.0].
        visit_count: Raw visit count for this vertex.
        total_visits: Total visit count across all vertices.
    """

    score: float
    visit_count: int
    total_visits: int


class ImportanceScorer:
    """Computes normalized importance from BFS visit counts.

    The importance of a vertex is proportional to how many times it was
    visited during multi-hop traversal. Vertices at the intersection of
    multiple reasoning chains accumulate higher visit counts.

    Usage::

        scorer = ImportanceScorer()
        visit_counts = {"v1": 5, "v2": 3, "v3": 2}
        result = scorer.compute("v1", visit_counts)
        logger.info("importance=%s", result.score)  # 0.5
    """

    def compute(
        self,
        vertex_id: str,
        visit_counts: dict[str, int],
    ) -> ImportanceResult:
        """Compute normalized importance for a single vertex.

        Args:
            vertex_id: The vertex to compute importance for.
            visit_counts: Dict mapping vertex IDs to visit counts.

        Returns:
            ImportanceResult with normalized score.

        Raises:
            ValueError: If visit_counts is empty or vertex_id not found (the
                latter only with ``importance_strict_inputs_enabled``; off, an
                unknown vertex scores 0.0).
        """
        if not visit_counts:
            msg = "visit_counts must be non-empty"
            raise ValueError(msg)
        if _strict_inputs() and vertex_id not in visit_counts:
            msg = f"vertex_id {vertex_id!r} is not in visit_counts"
            raise ValueError(msg)

        count = visit_counts.get(vertex_id, 0)
        total = sum(visit_counts.values())

        if total == 0:
            return ImportanceResult(score=0.0, visit_count=0, total_visits=0)

        score = count / total
        return ImportanceResult(
            score=score,
            visit_count=count,
            total_visits=total,
        )

    def compute_all(
        self,
        visit_counts: dict[str, int],
    ) -> dict[str, ImportanceResult]:
        """Compute normalized importance for all vertices in visit_counts.

        Args:
            visit_counts: Dict mapping vertex IDs to visit counts.

        Returns:
            Dict mapping vertex IDs to ImportanceResult.
        """
        if not visit_counts:
            return {}

        total = sum(visit_counts.values())
        if total == 0:
            return {vid: ImportanceResult(score=0.0, visit_count=0, total_visits=0) for vid in visit_counts}

        results: dict[str, ImportanceResult] = {}
        for vid, count in visit_counts.items():
            results[vid] = ImportanceResult(
                score=count / total,
                visit_count=count,
                total_visits=total,
            )
        return results

    def normalize_to_dict(
        self,
        visit_counts: dict[str, int],
    ) -> dict[str, float]:
        """Convenience: return {vertex_id: normalized_importance} dict.

        Args:
            visit_counts: Dict mapping vertex IDs to visit counts.

        Returns:
            Dict mapping vertex IDs to normalized importance floats.
        """
        results = self.compute_all(visit_counts)
        return {vid: r.score for vid, r in results.items()}


def compute_importance(visit_count: int, total_visits: int) -> float:
    """Convenience function for single-shot importance computation.

    Args:
        visit_count: Number of visits to this vertex (>= 0).
        total_visits: Total visits across all vertices (> 0).

    Returns:
        Normalized importance in [0.0, 1.0].

    Raises:
        ValueError: If total_visits <= 0 or visit_count < 0.
    """
    if visit_count < 0:
        msg = f"visit_count must be >= 0, got {visit_count}"
        raise ValueError(msg)
    if total_visits <= 0:
        msg = f"total_visits must be > 0, got {total_visits}"
        raise ValueError(msg)
    return visit_count / total_visits


def compile_importance_cypher(
    visit_count_prop: str = "visit_count",
    total_visits_param: str = "total_visits",
    default_when_null: float = 0.0,
) -> str:
    """Generate Cypher expression for Importance scoring dimension.

    The total_visits value is passed as a query parameter ($total_visits)
    since it requires aggregation across all candidates. With
    ``importance_strict_inputs_enabled`` a null or non-positive
    ``$total_visits`` scores ``default_when_null`` instead of dividing by it.

    Args:
        visit_count_prop: Node property name for raw visit count.
        total_visits_param: Query parameter name for total visits.
        default_when_null: Fallback value when visit_count is NULL.

    Returns:
        Cypher expression string.
    """
    visit_count_prop = sanitize_label(visit_count_prop)
    total_visits_param = sanitize_label(total_visits_param)
    default = cypher_number(default_when_null)
    if _strict_inputs():
        return (
            f"CASE WHEN candidate.{visit_count_prop} IS NULL OR ${total_visits_param} IS NULL "
            f"OR ${total_visits_param} <= 0 THEN {default} "
            f"ELSE toFloat(candidate.{visit_count_prop}) / ${total_visits_param} END"
        )
    return (
        f"CASE WHEN candidate.{visit_count_prop} IS NULL THEN {default} "
        f"ELSE toFloat(candidate.{visit_count_prop}) / ${total_visits_param} END"
    )
