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
from datetime import UTC, date, datetime
from typing import Any

from engine.causal.identity import FALLBACK_ID_PROPERTY, node_id_property
from engine.config.outcome_vocabulary import outcome_node_label
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

# Valid attribution model names
VALID_MODELS = frozenset({"first_touch", "last_touch", "linear", "position_based"})


def _unique_touchpoint_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """RU-067: one row per touchpoint id, keeping its first (minimum-distance) occurrence.

    The trace query returns one row per PATH ordered by ``distance ASC``; in a
    diamond or with parallel edges a touchpoint appears once per path. With
    ``causal_attribution_unique_touchpoints`` on, weights and temporal-decay
    ages are computed over the retained rows only. Off: rows as returned.
    """
    if not settings.causal_attribution_unique_touchpoints:
        return results
    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for row in results:
        tp_id = row.get("touchpoint_id")
        if not tp_id or tp_id in seen:
            continue
        seen.add(tp_id)
        unique.append(row)
    return unique


# SI-27: models whose result depends on touchpoint order.
_ORDERED_MODELS = frozenset({"first_touch", "last_touch", "position_based"})


class TouchpointTimestampConflictError(ValueError):
    """Touchpoint timestamps that cannot be ordered against each other (SI-27)."""


def _timestamp_key(value: Any) -> tuple[str, float] | None:
    """An orderable ``(kind, value)`` for a declared touchpoint timestamp, or ``None`` when absent/unusable.

    Temporal values (Neo4j DateTime / Date, ISO-8601 strings) order as POSIX
    seconds, a naive datetime read as UTC; numbers order as declared. The two
    kinds are never compared with each other (the caller refuses a mix).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return ("number", float(value)) if math.isfinite(float(value)) else None
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        value = to_native()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return ("temporal", moment.timestamp())
    if isinstance(value, date):
        return ("temporal", datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp())
    return None


def _time_ordered_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """SI-27: rows ordered by ``touchpoint_ts`` (oldest first) and the number excluded.

    A row with no usable timestamp is excluded and counted, never placed by
    distance. Equal timestamps order by touchpoint id (deterministic). A mix of
    temporal and numeric timestamps cannot be ordered and is refused.
    """
    keyed: list[tuple[tuple[str, float], dict[str, Any]]] = []
    excluded = 0
    for row in rows:
        if not row.get("touchpoint_id"):
            continue
        key = _timestamp_key(row.get("touchpoint_ts"))
        if key is None:
            excluded += 1
            continue
        keyed.append((key, row))
    kinds = {key[0] for key, _ in keyed}
    if len(kinds) > 1:
        msg = "touchpoint timestamps mix temporal and numeric values; declare one kind of source_timestamp_property"
        raise TouchpointTimestampConflictError(msg)
    keyed.sort(key=lambda item: (item[0][1], str(item[1]["touchpoint_id"])))
    return [row for _, row in keyed], excluded


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

    Given an outcome (the domain's outcome node), traces backward through
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

        depth = int(max_depth or self._spec.causal.chain_depth_limit)
        outcome_label = sanitize_label(outcome_node_label(self._spec, "causal attribution"))

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
        # SI-27: with causal_attribution_time_ordered on, order-dependent
        # models read each touchpoint's declared event timestamp.
        time_ordered = settings.causal_attribution_time_ordered and model in _ORDERED_MODELS
        timestamp_column = (
            f"       {self._touchpoint_timestamp_expression()} AS touchpoint_ts,\n" if time_ordered else ""
        )
        cypher = (
            f"MATCH (outcome:{outcome_label} {{outcome_id: $outcome_id}})\n"
            f"MATCH path = (touchpoint)-{rel_pattern}->(outcome)\n"
            f"RETURN {self._touchpoint_id_expression()} AS touchpoint_id,\n"
            f"{timestamp_column}"
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
            if time_ordered:
                return {"touchpoints": {}, "model": model, "chain_depth": 0, "untimestamped_touchpoints": 0}
            return {"touchpoints": {}, "model": model, "chain_depth": 0}

        retained = _unique_touchpoint_rows(results)
        untimestamped = 0
        if time_ordered:
            retained, untimestamped = _time_ordered_rows(retained)
            touchpoints = self._assign_weights(retained, model)
        else:
            touchpoints = self._assign_weights(results, model)

        response: dict[str, Any] = {
            "touchpoints": touchpoints,
            "model": model,
            "chain_depth": max((r["distance"] for r in results), default=0),
            "total_touchpoints": len(touchpoints),
        }
        if time_ordered:
            response["ordering"] = "event_timestamp"
            response["untimestamped_touchpoints"] = untimestamped

        # Temporal-decay refinement (feature-flagged, per-domain). Down-weights
        # touchpoints reached through older causal links and renormalizes so the
        # attribution weights still sum to 1.0. Flag off -> weights unchanged.
        if self._spec.causal.temporal_decay_enabled:
            halflife = settings.decay_transaction_halflife
            ages_by_touchpoint: dict[str, float] = {}
            for r in retained:
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

    def _touchpoint_id_expression(self) -> str:
        """The touchpoint's identity, per label (PO-04 / RU-068).

        ``touchpoint`` may carry any causal-edge source label; each label's
        declared id property is read via a CASE over those labels (sanitized,
        ``engine.causal.identity``), falling back to ``entity_id`` for labels the
        spec gives no declaration and for nodes that lack the declared property.
        Flag off: ``entity_id``.
        """
        if not settings.causal_edge_declared_identity:
            return "touchpoint.entity_id"
        branches: list[str] = []
        seen: set[str] = set()
        for edge in self._spec.causal.causal_edges:
            label = sanitize_label(edge.source_label)
            if label in seen:
                continue
            seen.add(label)
            prop = sanitize_label(node_id_property(self._spec, edge.source_label))
            if prop != FALLBACK_ID_PROPERTY:
                # Legacy nodes carrying only entity_id still resolve (as
                # handlers._candidate_id does for match candidates).
                branches.append(f"WHEN touchpoint:{label} THEN coalesce(touchpoint.{prop}, touchpoint.entity_id)")
        if not branches:
            return "touchpoint.entity_id"
        return f"CASE {' '.join(branches)} ELSE touchpoint.entity_id END"

    def _touchpoint_timestamp_expression(self) -> str:
        """SI-27: each touchpoint's declared event timestamp, per source label.

        A CASE over the causal-edge source labels that declare
        ``source_timestamp_property`` (labels and property names sanitized,
        C-9); a label with no declaration yields ``null``, so its touchpoints
        are excluded from ordering and counted. Two edges declaring different
        properties for one source label are refused.
        """
        declared: dict[str, str] = {}
        for edge in self._spec.causal.causal_edges:
            prop = getattr(edge, "source_timestamp_property", None)
            if prop is None or not str(prop).strip():
                continue
            label = sanitize_label(edge.source_label)
            prop = sanitize_label(prop)
            if declared.get(label, prop) != prop:
                msg = (
                    f"causal edges declare different source_timestamp_property values for source label {label!r}: "
                    f"{declared[label]!r} and {prop!r}"
                )
                raise TouchpointTimestampConflictError(msg)
            declared[label] = prop
        if not declared:
            return "null"
        branches: list[str] = []
        for source_label, timestamp_prop in declared.items():
            # Re-sanitized at the interpolation site (C-9; cypher-lint traces this form).
            branches.append(
                f"WHEN touchpoint:{sanitize_label(source_label)} THEN touchpoint.{sanitize_label(timestamp_prop)}"
            )
        return f"CASE {' '.join(branches)} ELSE null END"

    @staticmethod
    def _assign_weights(
        results: list[dict[str, Any]],
        model: str,
    ) -> dict[str, float]:
        """Assign attribution weights based on the selected model."""
        touchpoint_ids = [r["touchpoint_id"] for r in _unique_touchpoint_rows(results) if r["touchpoint_id"]]
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


# ── CANARY, DO NOT MERGE ──────────────────────────────────────────────────────
# Witness for the CEG consumer Semgrep rulepack (.semgrep/ceg-consumer-rules.yaml):
# one true positive per rule, so Analyze (central Core) must block on all four.
import os  # noqa: E402
import threading  # noqa: E402

import httpx  # noqa: E402
import structlog  # noqa: E402

_canary_structlog = structlog.get_logger(__name__)
_canary_instance: object | None = None
_canary_lock = threading.Lock()


def _canary_child_logger() -> object:
    # l9.no-structlog-getchild
    return _canary_structlog.getChild("canary")


def _canary_singleton() -> object:
    # l9.singleton-requires-lock
    global _canary_instance
    if _canary_instance is None:
        _canary_instance = object()
    return _canary_instance


def _canary_env_float() -> float:
    # l9.float-requires-try-except
    return float(os.environ["CEG_CANARY_THRESHOLD"])


async def _canary_leaked_client() -> None:
    # l9.httpx-async-context-required
    client = httpx.AsyncClient()
    await client.aclose()
