"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [health, services]
tags: [health, enrichment, roi, trigger]
owner: engine-team
status: active
--- /L9_META ---

Enrichment impact measurement (MatchQualityDelta).

The enrichment trigger that used to live here — ``compute_enrichment_priority``
(the ROI priority) and ``trigger_reenrichment_v2`` (the CEG -> Gate -> EIE
``enrich`` dispatch) — was retired: the trigger lives in EIE (operator decision
2026-11-15). EIE calls ``health-assess`` and decides itself what to enrich; CEG
sends no enrichment request.
"""

from __future__ import annotations

from typing import Any

from engine.health.field_health import EntityHealth, MatchQualityDelta


def measure_health_impact(
    entity_id: str,
    domain: str,
    health_before: EntityHealth,
    health_after: EntityHealth,
    *,
    enrichment_cost_usd: float = 0.0,
    enrichment_tokens: int = 0,
    match_outcomes_before: list[Any] | None = None,
    match_outcomes_after: list[Any] | None = None,
) -> MatchQualityDelta:
    """Measure the impact of enrichment on an entity's health and match quality."""
    avg_before = None
    avg_after = None
    improvement = None
    matches_before = len(match_outcomes_before) if match_outcomes_before else 0
    matches_after = len(match_outcomes_after) if match_outcomes_after else 0

    if match_outcomes_before:
        scores = [getattr(o, "score", 0) for o in match_outcomes_before]
        avg_before = sum(scores) / len(scores) if scores else None

    if match_outcomes_after:
        scores = [getattr(o, "score", 0) for o in match_outcomes_after]
        avg_after = sum(scores) / len(scores) if scores else None

    if avg_before is not None and avg_after is not None:
        improvement = avg_after - avg_before

    readiness_improvement = health_after.readiness_score - health_before.readiness_score
    gate_improvement = health_after.gate_completeness - health_before.gate_completeness

    roi = None
    if enrichment_cost_usd > 0 and readiness_improvement > 0:
        roi = readiness_improvement / enrichment_cost_usd

    return MatchQualityDelta(
        entity_id=entity_id,
        domain=domain,
        readiness_score_before=health_before.readiness_score,
        readiness_score_after=health_after.readiness_score,
        readiness_improvement=readiness_improvement,
        gate_pass_rate_before=health_before.gate_completeness,
        gate_pass_rate_after=health_after.gate_completeness,
        gate_pass_improvement=gate_improvement,
        avg_match_score_before=avg_before,
        avg_match_score_after=avg_after,
        match_score_improvement=improvement,
        matches_before_count=matches_before,
        matches_after_count=matches_after,
        matches_delta=matches_after - matches_before,
        enrichment_cost_usd=enrichment_cost_usd,
        enrichment_tokens=enrichment_tokens,
        roi=roi,
    )
