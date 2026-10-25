"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [health, api]
tags: [health, api, endpoints]
owner: engine-team
status: active
--- /L9_META ---

HEALTH service API handler — integrates with chassis action routing.
Three logical endpoints routed via payload["sub_action"]:
  - assess      → single entity health assessment
  - batch_assess → incremental batch scan with cost ceilings
  - report      → health report for an entity

Capability resolution (HEALTH-02 / COMM-01 / COMM-02): every subaction asks
``resolve_effective_capabilities`` what it may do. The request's ``tier`` is
echoed back and never grants anything; upgrade prompts and conversion events
exist only while ``health_commercial_mode_enabled`` is on.

Identity (HEALTH-05): tenant id == domain spec id == Neo4j database name.
``payload.domain`` may name the packet tenant or be absent; any other value is
rejected. There is no mapping layer.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config.loader import DomainPackLoader
from engine.health.domain_field_mapper import build_field_map
from engine.health.enrichment_trigger import compute_enrichment_priority, trigger_reenrichment_v2
from engine.health.field_analyzer import analyze_entity_fields
from engine.health.field_health import EntityHealth
from engine.health.gap_prioritizer import prioritize_gaps_v2
from engine.health.health_report import generate_health_report, track_conversion_event
from engine.health.health_schemas import (
    BatchHealthReport,
    BatchHealthRequest,
    EffectiveHealthCapabilities,
    EntityHealthResponse,
    HealthAssessmentRequest,
    HealthScanProfile,
    resolve_effective_capabilities,
)
from engine.health.readiness_scorer import compute_readiness_score_v2

logger = logging.getLogger(__name__)

# Module-level domain loader (initialized lazily)
_domain_loader: DomainPackLoader | None = None


def _get_domain_loader() -> DomainPackLoader:
    """Lazy-init domain loader."""
    global _domain_loader
    if _domain_loader is None:
        _domain_loader = DomainPackLoader()
    return _domain_loader


def resolve_domain(tenant: str, requested_domain: str | None) -> str:
    """HEALTH-05: the domain a health call may read is the packet tenant.

    Tenant id, domain spec id and Neo4j database name are one identity
    (operator decision 2026-10-22; no mapping layer). A ``payload.domain`` that
    names anything else is a cross-tenant read and is rejected. ``handle_admin``
    applies the same check before dispatching here; this one covers direct
    callers of this module.
    """
    if requested_domain is None or requested_domain == tenant:
        return tenant
    msg = (
        f"payload.domain {requested_domain!r} does not match the packet tenant {tenant!r}; "
        "health subactions read only the tenant's own domain (tenant id == spec id)"
    )
    raise ValueError(msg)


async def handle_health_assess(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /assess — Single entity health assessment.

    Capabilities come from the server-side resolver, not from ``payload.tier``.
    When auto-enrichment is attempted, the full dispatch outcome is returned in
    ``enrichment_dispatch`` (HEALTH-03).
    """
    request = HealthAssessmentRequest(**payload)
    domain = resolve_domain(tenant, request.domain)
    caps = resolve_effective_capabilities(request.tier)
    tier = caps.tier

    domain_spec = _get_domain_loader().load_domain(domain)

    # Analyze fields
    field_health = analyze_entity_fields(
        entity=request.entity_data,
        domain_spec=domain_spec,
        node_label=request.node_label,
        field_confidences=request.field_confidences,
    )

    # Compute readiness score
    readiness = compute_readiness_score_v2(field_health, domain_spec)

    # Build field map for inference rules
    field_map = build_field_map(domain_spec, request.node_label)

    # Prioritize gaps
    enrichment_targets = prioritize_gaps_v2(field_health, domain_spec, inference_rules=field_map.inference_rules)

    # Build EntityHealth
    critical_gaps = [f.field_name for f in field_health if f.is_gate_critical and not f.is_populated]
    entity_health = EntityHealth(
        entity_id=request.entity_id,
        domain=domain,
        readiness_score=readiness.overall_score,
        grade=readiness.grade,
        field_health=field_health,
        critical_gaps=critical_gaps,
        enrichment_targets=enrichment_targets if caps.allows("enrichment_targets") else [],
        gate_completeness=readiness.gate_completeness,
        scoring_dimension_coverage=readiness.scoring_dimension_coverage,
        inference_unlock_potential=readiness.inference_unlock_potential,
        recommended_actions=_build_action_strings(critical_gaps, caps),
        next_enrichment_priority=_classify_priority(readiness.overall_score),  # type: ignore[arg-type]
    )

    # Enrichment priority when the capability set includes targets
    enrichment_priority = None
    if caps.allows("enrichment_targets"):
        enrichment_priority = compute_enrichment_priority(entity_health, domain_spec)

    # Auto-enrich: surface the whole dispatch result, not just that it was attempted.
    enrichment_dispatch: dict[str, Any] | None = None
    if caps.allows("auto_enrich") and enrichment_priority and enrichment_priority.recommendation == "enrich_now":
        enrichment_dispatch = await trigger_reenrichment_v2(entity_health, domain_spec, tenant)

    # Conversion funnel — commercial mode only (COMM-02)
    upgrade_prompt = None
    if caps.conversion_tracking:
        track_conversion_event(tenant, request.entity_id, "health_viewed")
        if critical_gaps:
            track_conversion_event(tenant, request.entity_id, "gaps_shown")
            if caps.upgrade_prompts:
                upgrade_prompt = (
                    f"You have {len(critical_gaps)} gate-critical gaps blocking matches. "
                    f"Upgrade to Enrich to auto-fill them and unlock {len(enrichment_targets)} "
                    f"enrichment opportunities."
                )
                track_conversion_event(
                    tenant,
                    request.entity_id,
                    "upgrade_prompted",
                    metadata={"gaps": len(critical_gaps), "targets": len(enrichment_targets)},
                )

    response = EntityHealthResponse(
        entity_health=entity_health,
        enrichment_priority=enrichment_priority,
        tier=tier,
        requested_tier=caps.requested_tier,
        commercial_mode=caps.commercial_mode,
        upgrade_prompt=upgrade_prompt,
        enrichment_dispatch=enrichment_dispatch,
    )

    return response.model_dump()


def _bounded(requested: float | None, ceiling: float) -> float:
    """BATCH-01: a request may lower a ceiling, never raise it."""
    if requested is None:
        return ceiling
    return min(requested, ceiling)


async def handle_health_batch_assess(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /batch-assess — Incremental batch scan with cost ceilings.

    Uses HealthScanProfile for delta-based targeting. ``max_entities`` and
    ``cost_ceiling_usd`` are bounded by the server-owned ceilings in Settings
    (BATCH-01); the report's buckets are defined on ``BatchHealthReport``
    (BATCH-02). No enrichment is dispatched here, so ``dispatched`` is 0.
    """
    from engine.config.settings import settings

    request = BatchHealthRequest(**payload)
    domain = resolve_domain(tenant, request.domain)
    caps = resolve_effective_capabilities(request.tier)
    tier = caps.tier

    if not caps.allows("batch_scan"):
        msg = f"Batch scan is not available to the effective tier {tier!r} (requires discover or above)"
        raise ValueError(msg)

    cost_ceiling = _bounded(request.cost_ceiling_usd, settings.health_batch_cost_ceiling_usd)
    max_entities = int(_bounded(request.max_entities, settings.health_batch_max_entities))
    scan_profile = request.scan_profile or HealthScanProfile()

    domain_spec = _get_domain_loader().load_domain(domain)

    # In production, entity_ids would come from Neo4j query filtered by scan_profile.
    # For now, process entities provided in payload.
    entities = request.entities
    window = entities[:max_entities]
    results: list[EntityHealthResponse] = []
    total_cost = 0.0
    estimated_eligible = 0
    total_skipped = 0
    ceiling_reached = False

    for entity_data in window:
        if total_cost >= cost_ceiling:
            ceiling_reached = True
            break

        entity_id = entity_data.get("entity_id", "unknown")
        node_label = entity_data.get("node_label", "")

        field_health = analyze_entity_fields(
            entity=entity_data.get("data", {}),
            domain_spec=domain_spec,
            node_label=node_label,
        )
        readiness = compute_readiness_score_v2(field_health, domain_spec)
        field_map = build_field_map(domain_spec, node_label)
        enrichment_targets = prioritize_gaps_v2(field_health, domain_spec, inference_rules=field_map.inference_rules)

        critical_gaps = [f.field_name for f in field_health if f.is_gate_critical and not f.is_populated]
        entity_health = EntityHealth(
            entity_id=entity_id,
            domain=domain,
            readiness_score=readiness.overall_score,
            grade=readiness.grade,
            field_health=field_health,
            critical_gaps=critical_gaps,
            enrichment_targets=enrichment_targets,
            gate_completeness=readiness.gate_completeness,
            scoring_dimension_coverage=readiness.scoring_dimension_coverage,
        )

        priority = compute_enrichment_priority(entity_health, domain_spec)

        if priority.recommendation != "skip":
            # The ceiling bounds what this batch may commit to, so an eligible
            # entity is admitted only if its estimate still fits; otherwise it
            # stays unassessed (counted from the window below) and cost_usd
            # never exceeds the server-owned ceiling.
            if total_cost + priority.estimated_cost_usd > cost_ceiling:
                ceiling_reached = True
                break
            total_cost += priority.estimated_cost_usd
            estimated_eligible += 1
        else:
            total_skipped += 1

        results.append(
            EntityHealthResponse(
                entity_health=entity_health,
                enrichment_priority=priority,
                tier=tier,
                requested_tier=caps.requested_tier,
                commercial_mode=caps.commercial_mode,
            )
        )

    # Entities inside the window that the cost ceiling stopped us from assessing
    # are skipped — counted once, from the window, never from the whole request
    # (the previous ``len(entities) - len(results)`` also counted truncated ones).
    total_skipped += len(window) - len(results)

    report = BatchHealthReport(
        domain=domain,
        tier=tier,
        requested_tier=caps.requested_tier,
        total_candidates=len(entities),
        total_scanned=len(results),
        estimated_eligible=estimated_eligible,
        dispatched=0,
        total_skipped=total_skipped,
        total_truncated=len(entities) - len(window),
        cost_usd=total_cost,
        cost_ceiling_usd=cost_ceiling,
        max_entities=max_entities,
        cost_ceiling_reached=ceiling_reached,
        entities=results,
        scan_profile=scan_profile,
    )

    return report.model_dump()


async def handle_health_report(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """GET /report/{entity_id} — full health report for an entity.

    Upgrade recommendations and conversion events appear only in commercial
    mode (COMM-02); the report body itself is the same for every caller.
    """
    entity_id = payload.get("entity_id", "")
    domain = resolve_domain(tenant, payload.get("domain"))
    caps = resolve_effective_capabilities(payload.get("tier"))

    domain_spec = _get_domain_loader().load_domain(domain)

    entity_data = payload.get("entity_data", {})
    node_label = payload.get("node_label", "")

    field_health = analyze_entity_fields(
        entity=entity_data,
        domain_spec=domain_spec,
        node_label=node_label,
    )
    readiness = compute_readiness_score_v2(field_health, domain_spec)
    field_map = build_field_map(domain_spec, node_label)
    enrichment_targets = prioritize_gaps_v2(field_health, domain_spec, inference_rules=field_map.inference_rules)

    critical_gaps = [f.field_name for f in field_health if f.is_gate_critical and not f.is_populated]
    entity_health = EntityHealth(
        entity_id=entity_id,
        domain=domain,
        readiness_score=readiness.overall_score,
        grade=readiness.grade,
        field_health=field_health,
        critical_gaps=critical_gaps,
        enrichment_targets=enrichment_targets,
        gate_completeness=readiness.gate_completeness,
        scoring_dimension_coverage=readiness.scoring_dimension_coverage,
    )

    report = generate_health_report(entity_health, tier=caps.tier, capabilities=caps)

    # Conversion funnel — commercial mode only
    if caps.conversion_tracking:
        track_conversion_event(tenant, entity_id, "health_viewed")
        if critical_gaps:
            track_conversion_event(tenant, entity_id, "gaps_shown")
            if caps.upgrade_prompts:
                track_conversion_event(tenant, entity_id, "upgrade_prompted")

    return report.model_dump()


def _build_action_strings(critical_gaps: list[str], caps: EffectiveHealthCapabilities) -> list[str]:
    """Build human-readable action strings."""
    actions: list[str] = []
    if critical_gaps:
        actions.append(f"Fill {len(critical_gaps)} gate-critical fields: {', '.join(critical_gaps[:5])}")
    if caps.upgrade_prompts and caps.tier == "seed":
        actions.append("Upgrade to Enrich tier for automated gap filling")
    return actions


def _classify_priority(readiness_score: float) -> str:
    """Classify enrichment priority from readiness score."""
    if readiness_score < 30:
        return "urgent"
    if readiness_score < 50:
        return "high"
    if readiness_score < 70:
        return "normal"
    return "low"
