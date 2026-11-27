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
4. ``pareto_precut()`` / ``precut_pool_limit()`` — SI-06: order a bounded
   pool by Pareto front before the ``top_n`` cut (``pareto_precut_enabled``)
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from engine.scoring.pareto import ParetoCandidate, ParetoFront, compute_pareto_front, non_dominated_ranks

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


def _numeric_dimension_value(record: dict[str, Any], dim: str) -> float | None:
    """The dimension's value as a finite float, or ``None`` when absent or not a number.

    SI-06: ``None`` stays unknown — nothing here substitutes ``0.0`` or ``inf``.
    A ``bool`` is not a score, and ``NaN`` orders against nothing.
    """
    raw = _lookup_dimension_score(record, dim)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


# ── Candidate construction ───────────────────────────────────────────────────


def build_pareto_candidates(
    results: list[dict[str, Any]],
    dimension_names: list[str],
    *,
    candidate_id_key: str = "entity_id",
    directions: Mapping[str, str] | None = None,
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
    directions:
        RU-019: resolved ``{dimension: "minimize" | "maximize"}``. A missing or
        non-numeric value on a ``"minimize"`` dimension is filled with
        ``math.inf`` (the worst possible cost) instead of ``0.0``, which
        dominance would negate into the best possible value. ``None`` keeps the
        ``0.0`` fill for every dimension.

    Returns
    -------
    list[ParetoCandidate]
    """
    from engine.config.settings import settings

    zero_present = settings.arbitration_zero_score_present
    # SI-06: under pareto_precut_enabled an absent or non-numeric value is left
    # out of dimension_scores instead of being filled; callers treat the
    # candidate as unranked (L9-UNKNOWN-001: an unknown is never a 0.0 score).
    omit_missing = settings.pareto_precut_enabled
    minimized = {d for d, v in (directions or {}).items() if v == "minimize"}
    candidates: list[ParetoCandidate] = []
    for idx, record in enumerate(results):
        cand_dict = record.get("candidate", {})
        cand_id = str(cand_dict.get(candidate_id_key, idx)) if isinstance(cand_dict, dict) else str(idx)

        dim_scores: dict[str, float] = {}
        for dim in dimension_names:
            if omit_missing:
                value = _numeric_dimension_value(record, dim)
                if value is not None:
                    dim_scores[dim] = value
                continue
            if zero_present:
                # RU-018: 0.0 is a score; only an absent / None value is missing.
                # RU-019: a missing minimised value is the worst cost, never the best.
                missing = math.inf if dim in minimized else 0.0
                present = _lookup_dimension_score(record, dim)
                if present is None:
                    dim_scores[dim] = missing
                    continue
                try:
                    dim_scores[dim] = float(present)
                except (TypeError, ValueError):
                    dim_scores[dim] = missing
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
                    try:
                        dim_scores[dim] = float(ds[dim])
                    except (TypeError, ValueError):
                        dim_scores[dim] = 0.0
                else:
                    dim_scores[dim] = 0.0

        candidates.append(ParetoCandidate(candidate_id=cand_id, dimension_scores=dim_scores))

    return candidates


# ── Constraint enforcement ───────────────────────────────────────────────────


def apply_constraint_penalties(
    candidates: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    *,
    stats: dict[str, int] | None = None,
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

    stats:
        SCR-03: when given, ``stats["unknown_rejected"]`` receives the number of
        candidates a hard constraint rejected for an unknown value (flag on only).

    Returns
    -------
    Filtered list — hard constraint violations removed, soft penalties applied.

    Under ``arbitration_unknown_fails_constraint_enabled`` (SCR-03 / SCR-04, rule
    Q2) a missing, None, non-numeric (``bool`` included) or NaN value fails the
    constraint, and the penalised score is ``min(score, score * penalty)`` with a
    None score left as None.
    """
    from engine.config.settings import settings

    if settings.arbitration_unknown_fails_constraint_enabled:
        return _apply_constraints_unknown_fails(candidates, constraints, stats)
    zero_present = settings.arbitration_zero_score_present
    filtered: list[dict[str, Any]] = []

    for candidate in candidates:
        rejected = False
        ds = candidate.get("dimension_scores", {})
        if not isinstance(ds, dict):
            ds = {}

        for constraint in constraints:
            dim = constraint.get("dimension", "")
            # threshold / penalty come from ArbitrationConstraintSpec (pydantic float,
            # ge=0.0, le=1.0), copied verbatim by handle_match, the only caller.
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

            # A non-numeric result-row value is treated like an absent one,
            # as build_pareto_candidates does.
            try:
                score = float(score)
            except (TypeError, ValueError):
                continue

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


def _raw_source_known(candidate: dict[str, Any], prop: str) -> bool:
    """CEG-A07: the candidate's stored source is a number, as ``toFloatOrNull`` reads it.

    Missing, None, NaN, bool and any non-numeric value are unknown; a numeric
    string parses, matching the Cypher pass.
    """
    node = candidate.get("candidate")
    raw = node.get(prop) if isinstance(node, dict) else None
    if raw is None or isinstance(raw, bool):
        return False
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            return False
    if not isinstance(raw, (int, float)):
        return False
    return not math.isnan(float(raw))


def _known_dimension_value(
    candidate: dict[str, Any], dim: str, source_properties: list[str] | None = None
) -> float | None:
    """The constrained value as a finite-or-infinite float, or None when unknown (SCR-03).

    CEG-A07: when the dimension reads raw candidate properties, each must be a
    known number; the dimension score alone may be a default or a clamp.
    """
    if source_properties and not all(_raw_source_known(candidate, p) for p in source_properties):
        return None
    raw = _lookup_dimension_score(candidate, dim)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return None if math.isnan(value) else value


def _apply_constraints_unknown_fails(
    candidates: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    stats: dict[str, int] | None,
) -> list[dict[str, Any]]:
    """SCR-03 / SCR-04: unknown fails the constraint; a penalty never raises a score."""
    filtered: list[dict[str, Any]] = []
    unknown_rejected = 0
    for candidate in candidates:
        rejected = False
        for constraint in constraints:
            dim = constraint.get("dimension", "")
            threshold = float(constraint.get("threshold", 0.0))
            value = _known_dimension_value(candidate, dim, constraint.get("source_properties"))
            if value is not None and value >= threshold:
                continue
            if constraint.get("hard", False):
                rejected = True
                unknown_rejected += value is None
                break
            score = candidate.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                candidate["score"] = min(score, score * float(constraint.get("penalty", 0.5)))
        if not rejected:
            filtered.append(candidate)
    if stats is not None:
        stats["unknown_rejected"] = unknown_rejected
    logger.info(
        "Constraint enforcement: %d/%d candidates passed (%d rejected, %d for an unknown value)",
        len(filtered),
        len(candidates),
        len(candidates) - len(filtered),
        unknown_rejected,
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

    pareto_candidates = build_pareto_candidates(
        candidates,
        dimension_names,
        candidate_id_key=candidate_id_key,
        directions=resolved_directions,
    )

    if not pareto_candidates:
        return {
            "nondominated": [],
            "dominated": [],
            "trimmed": [],
            "frontsize": 0,
            "pruned_pct": 0.0,
        }

    # SI-06: a candidate missing an objective cannot be placed on or off the
    # front without inventing that value, so it is reported, not compared.
    unranked: list[ParetoCandidate] = []
    comparable = pareto_candidates
    if settings.pareto_precut_enabled:
        wanted = set(dimension_names)
        comparable = [c for c in pareto_candidates if set(c.dimension_scores) == wanted]
        unranked = [c for c in pareto_candidates if set(c.dimension_scores) != wanted]

    # SC-004: compare the declared objectives, not whichever keys the first row carries.
    front: ParetoFront = compute_pareto_front(
        comparable, directions=resolved_directions, dimension_names=list(dimension_names)
    )

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

    filtered: dict[str, Any] = {
        "nondominated": nondominated_ids,
        "dominated": dominated_ids,
        "trimmed": trimmed_ids,
        "frontsize": front_size,
        "pruned_pct": pruned_pct,
    }
    if settings.pareto_precut_enabled:
        filtered["unranked"] = [c.candidate_id for c in unranked]
    return filtered


# ── Pareto pre-cut (SI-06) ───────────────────────────────────────────────────

# The pool handle_match fetches when pareto_precut_enabled: PRECUT_POOL_MULTIPLIER
# times top_n, never more than PRECUT_POOL_CEILING rows and never fewer than
# top_n. The ceiling equals handle_match's own top_n cap (1000), bounding the
# O(n² · d) front sort and its O(n²) dominance lists.
PRECUT_POOL_MULTIPLIER = 4
PRECUT_POOL_CEILING = 1000


def precut_pool_limit(top_n: int) -> int:
    """Rows to fetch so Pareto arbitration sees more than the ``top_n`` it returns."""
    return max(top_n, min(top_n * PRECUT_POOL_MULTIPLIER, PRECUT_POOL_CEILING))


def _composite_score(record: dict[str, Any]) -> float | None:
    raw = record.get("score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or math.isnan(raw):
        return None
    return float(raw)


def _score_order(score: float | None) -> tuple[int, float]:
    """Higher composite score first; a row without a numeric score after every scored one."""
    return (1, 0.0) if score is None else (0, -score)


def pareto_precut(
    candidates: list[dict[str, Any]],
    dimension_names: list[str],
    *,
    top_n: int,
    pool_limit: int,
    candidate_id_key: str = "entity_id",
    directions: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Order a candidate pool by Pareto front over ``dimension_names``, then cut to ``top_n``.

    Order (deterministic for a given pool):

    1. Rows carrying every objective, by front rank ascending (fast
       non-dominated sort; rank 0 is the non-dominated set).
    2. Within a rank: composite ``score`` descending, then candidate id
       ascending, then pool position.
    3. Rows missing any objective (``unranked``) after every ranked row, by the
       same score / id / position key. Their missing values are never filled.

    Directions follow ``apply_pareto_filter``: honoured only while
    ``arbitration_objective_direction`` is on. ``front_size_limit`` is not
    applied: the ``top_n`` cut bounds the output.

    Returns the kept rows and the ``arbitration`` metadata block.
    """
    from engine.config.settings import settings

    resolved = _resolve_directions(directions) if settings.arbitration_objective_direction else None
    dims = sorted(set(dimension_names))

    ids: list[str] = []
    vectors: list[dict[str, float] | None] = []
    for idx, record in enumerate(candidates):
        cand_dict = record.get("candidate", {})
        ids.append(str(cand_dict.get(candidate_id_key, idx)) if isinstance(cand_dict, dict) else str(idx))
        values: dict[str, float] = {}
        for dim in dims:
            value = _numeric_dimension_value(record, dim)
            if value is None:
                break
            values[dim] = value
        # A row missing any objective stays unranked: its vector is not built.
        vectors.append(values if len(values) == len(dims) else None)

    ranked_positions = [i for i, v in enumerate(vectors) if v is not None]
    ranked_vectors = [v for v in vectors if v is not None]
    ranks_of_ranked = non_dominated_ranks(ranked_vectors, dims, resolved)
    rank: dict[int, int] = dict(zip(ranked_positions, ranks_of_ranked, strict=True))

    def _key(position: int) -> tuple[int, int, int, float, str, int]:
        bucket, score = _score_order(_composite_score(candidates[position]))
        if position in rank:
            return (0, rank[position], bucket, score, ids[position], position)
        return (1, 0, bucket, score, ids[position], position)

    order = sorted(range(len(candidates)), key=_key)
    kept_positions = order[:top_n]

    front_ids = [ids[i] for i in order if rank.get(i) == 0]
    total = len(candidates)
    meta: dict[str, Any] = {
        "frontsize": len(front_ids),
        "pruned_pct": round((1.0 - len(front_ids) / total) * 100, 1) if total else 0.0,
        "nondominated_ids": front_ids,
        "precut": {
            "pool_limit": pool_limit,
            "pool_size": total,
            "returned": len(kept_positions),
            "cut_count": total - len(kept_positions),
            "front_ranks": [rank.get(i) for i in kept_positions],
            "unranked_ids": [ids[i] for i in order if i not in rank],
        },
    }
    logger.info(
        "Pareto pre-cut: pool %d (limit %d) -> %d returned, %d on front 0, %d unranked",
        total,
        pool_limit,
        len(kept_positions),
        len(front_ids),
        total - len(ranked_positions),
    )
    return [candidates[i] for i in kept_positions], meta
