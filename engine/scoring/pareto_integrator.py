"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [pareto, multi-objective, integration]
owner: engine-team
status: active
--- /L9_META ---

engine/scoring/pareto_integrator.py
Milestone 2.1 — Pareto Core Integration

Convenience wrapper around ``engine.scoring.pareto`` that bridges the
DomainSpec-level ``DecisionArbitrationSpec`` with the low-level Pareto
primitives.  Provides:

1. ``apply_pareto_filter()`` — Pareto-dominance filter for match candidates
2. ``build_pareto_candidates()`` — converts raw Neo4j result dicts to
   ``ParetoCandidate`` instances using dimension names from the spec
3. ``apply_constraint_penalties()`` — enforces hard/soft constraints from
   the ``DecisionArbitrationSpec``
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from engine.scoring.pareto import ParetoCandidate, ParetoFront, compute_pareto_front

logger = logging.getLogger(__name__)


# ── Dimension lookup ─────────────────────────────────────────────────────────


def _lookup_dimension_score(record: dict[str, Any], dim: str) -> Any:
    """Return the dimension's value from a result row, or ``None`` when absent.

    RU-018 / CEG-COR-025: lookup order is ``dimension_scores[dim]`` ->
    top-level ``dim`` -> top-level ``f"{dim}_score"``, and each step tests
    ``is None`` rather than truthiness, so a genuine ``0.0`` is a score that
    constraints and dominance evaluate like any other value.
    """
    ds = record.get("dimension_scores")
    if isinstance(ds, dict):
        value = ds.get(dim)
        if value is not None:
            return value
    value = record.get(dim)
    if value is not None:
        return value
    return record.get(f"{dim}_score")


# ── Candidate construction ───────────────────────────────────────────────────


def build_pareto_candidates(
    results: list[dict[str, Any]],
    dimension_names: list[str],
    *,
    candidate_id_key: str = "entity_id",
) -> list[ParetoCandidate]:
    """Convert raw Neo4j result dicts into ``ParetoCandidate`` instances.

    Parameters
    ----------
    results:
        List of result dicts from graph query.  Each dict should contain
        a ``"candidate"`` sub-dict with ``entity_id``, plus top-level keys
        matching ``dimension_names`` (e.g. ``structural_score``).
    dimension_names:
        Scoring dimension names from the DomainSpec (e.g. ``["structural",
        "geo_decay", "price_alignment"]``).
    candidate_id_key:
        Key within the ``"candidate"`` sub-dict to use as the candidate ID.

    Returns
    -------
    list[ParetoCandidate]
    """
    from engine.config.settings import settings

    zero_present = settings.arbitration_zero_score_present
    candidates: list[ParetoCandidate] = []
    for idx, record in enumerate(results):
        cand_dict = record.get("candidate", {})
        cand_id = str(cand_dict.get(candidate_id_key, idx)) if isinstance(cand_dict, dict) else str(idx)

        dim_scores: dict[str, float] = {}
        for dim in dimension_names:
            if zero_present:
                # RU-018: 0.0 is a score; only an absent / None value is missing.
                present = _lookup_dimension_score(record, dim)
                if present is None:
                    dim_scores[dim] = 0.0
                    continue
                try:
                    dim_scores[dim] = float(present)
                except (TypeError, ValueError):
                    dim_scores[dim] = 0.0
                continue
            # Legacy (C-21 rollback): truthiness fall-through substitutes a 0.0.
            # Try both "dim" and "dim_score" keys
            val = record.get(dim) or record.get(f"{dim}_score")
            if val is not None:
                try:
                    dim_scores[dim] = float(val)
                except (TypeError, ValueError):
                    dim_scores[dim] = 0.0
            else:
                # Check inside dimension_scores sub-dict
                ds = record.get("dimension_scores", {})
                if isinstance(ds, dict) and dim in ds:
                    dim_scores[dim] = float(ds[dim])
                else:
                    dim_scores[dim] = 0.0

        candidates.append(ParetoCandidate(candidate_id=cand_id, dimension_scores=dim_scores))

    return candidates


# ── Constraint enforcement ───────────────────────────────────────────────────


def apply_constraint_penalties(
    candidates: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply hard/soft constraint penalties from DecisionArbitrationSpec.

    Parameters
    ----------
    candidates:
        Raw result dicts with dimension scores.
    constraints:
        List of constraint dicts from ``DecisionArbitrationSpec.constraints``.
        Each has: ``dimension``, ``threshold``, ``hard`` (bool), and optional
        ``penalty`` (float, default 0.5).

    Returns
    -------
    Filtered list — hard constraint violations removed, soft penalties applied.
    """
    from engine.config.settings import settings

    zero_present = settings.arbitration_zero_score_present
    filtered: list[dict[str, Any]] = []

    for candidate in candidates:
        rejected = False
        ds = candidate.get("dimension_scores", {})
        if not isinstance(ds, dict):
            ds = {}

        for constraint in constraints:
            dim = constraint.get("dimension", "")
            threshold = float(constraint.get("threshold", 0.0))
            is_hard = constraint.get("hard", False)
            penalty = float(constraint.get("penalty", 0.5))

            if zero_present:
                # RU-018: dimension_scores[d] -> d -> d_score, presence by `is None`.
                score = _lookup_dimension_score(candidate, dim)
            else:
                score = ds.get(dim)
                if score is None:
                    # Also check top-level keys
                    score = candidate.get(dim) or candidate.get(f"{dim}_score")

            if score is None:
                continue

            score = float(score)

            if score < threshold:
                if is_hard:
                    rejected = True
                    break
                # Apply soft penalty to the overall score
                if "score" in candidate:
                    candidate["score"] = candidate["score"] * penalty

        if not rejected:
            filtered.append(candidate)

    logger.info(
        "Constraint enforcement: %d/%d candidates passed (%d rejected)",
        len(filtered),
        len(candidates),
        len(candidates) - len(filtered),
    )

    return filtered


# ── Pareto filter ────────────────────────────────────────────────────────────


def _resolve_directions(directions: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Normalise declared objective directions; anything but 'minimize' maximises.

    ``ParetoObjectiveSpec.direction`` is a free ``str`` in the schema, so an
    unrecognised value is logged and treated as ``"maximize"``.
    """
    if not directions:
        return None
    resolved: dict[str, str] = {}
    for dim, direction in directions.items():
        if direction == "minimize":
            resolved[dim] = "minimize"
            continue
        if direction != "maximize":
            logger.warning(
                "Arbitration objective %s: unknown direction %r treated as 'maximize'",
                dim,
                direction,
            )
        resolved[dim] = "maximize"
    return resolved


def apply_pareto_filter(
    candidates: list[dict[str, Any]],
    dimension_names: list[str],
    *,
    candidate_id_key: str = "entity_id",
    directions: Mapping[str, Any] | None = None,
    front_size_limit: int | None = None,
) -> dict[str, Any]:
    """Apply Pareto dominance filtering to a candidate set.

    Parameters
    ----------
    candidates:
        Raw result dicts from graph query.
    dimension_names:
        Scoring dimension names from the DomainSpec.
    candidate_id_key:
        Key within the ``"candidate"`` sub-dict for the ID.
    directions:
        RU-019: optional ``{dimension: "minimize" | "maximize"}`` from
        ``ParetoObjectiveSpec.direction``. Default maximises every dimension.
    front_size_limit:
        RU-019: optional ``ParetoConfigSpec.front_size_limit``. When the
        non-dominated set is larger, the candidates with the highest composite
        ``score`` are kept (R1b-DEC-04 A); the rest are reported as ``trimmed``.

    Both keyword arguments are ignored while
    ``settings.arbitration_objective_direction`` is off (C-21 rollback).

    Returns
    -------
    dict with keys:
        ``nondominated`` — list of non-dominated candidate IDs (at most
        ``front_size_limit``, input order preserved)
        ``dominated`` — list of dominated candidate IDs
        ``trimmed`` — non-dominated IDs dropped by ``front_size_limit``
        ``frontsize`` — number of candidates reported on the front
        ``pruned_pct`` — percentage of candidates pruned
    """
    from engine.config.settings import settings

    if settings.arbitration_objective_direction:
        resolved_directions = _resolve_directions(directions)
        limit = (
            front_size_limit if isinstance(front_size_limit, int) and not isinstance(front_size_limit, bool) else None
        )
    else:
        resolved_directions = None
        limit = None

    pareto_candidates = build_pareto_candidates(candidates, dimension_names, candidate_id_key=candidate_id_key)

    if not pareto_candidates:
        return {
            "nondominated": [],
            "dominated": [],
            "trimmed": [],
            "frontsize": 0,
            "pruned_pct": 0.0,
        }

    front: ParetoFront = compute_pareto_front(pareto_candidates, directions=resolved_directions)

    kept = list(front.non_dominated)
    trimmed_ids: list[str] = []
    if limit is not None and limit >= 1 and len(kept) > limit:
        # build_pareto_candidates is positional: pareto_candidates[i] <-> candidates[i].
        position = {id(c): i for i, c in enumerate(pareto_candidates)}

        def _composite(cand: ParetoCandidate) -> float:
            raw = candidates[position[id(cand)]].get("score")
            return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0

        ranked = sorted(kept, key=_composite, reverse=True)  # stable: ties keep input order
        keep_ids = {id(c) for c in ranked[:limit]}
        trimmed_ids = [c.candidate_id for c in kept if id(c) not in keep_ids]
        kept = [c for c in kept if id(c) in keep_ids]

    nondominated_ids = [c.candidate_id for c in kept]
    dominated_ids = [c.candidate_id for c in front.dominated]
    total = len(pareto_candidates)
    front_size = len(kept)
    pruned_pct = round((1.0 - front_size / total) * 100, 1) if total else 0.0

    logger.info(
        "Pareto filter: %d/%d on front (%.1f%% pruned, %d trimmed by front_size_limit)",
        front_size,
        total,
        pruned_pct,
        len(trimmed_ids),
    )

    return {
        "nondominated": nondominated_ids,
        "dominated": dominated_ids,
        "trimmed": trimmed_ids,
        "frontsize": front_size,
        "pruned_pct": pruned_pct,
    }
