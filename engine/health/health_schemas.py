"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [health, schemas]
tags: [health, api, schemas]
owner: engine-team
status: active
--- /L9_META ---

Request/response schemas for HEALTH service API.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from engine.health.field_health import EnrichmentPriority, EntityHealth, MatchQualityDelta

logger = logging.getLogger(__name__)

HealthTier = Literal["seed", "enrich", "discover", "autonomous"]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TIER FEATURES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TIER_FEATURES: dict[str, dict[str, Any]] = {
    "seed": {
        "readiness_score": True,
        "field_gaps": True,
        "enrichment_targets": False,
        "auto_enrich": False,
        "batch_scan": False,
        "conversion_tracking": True,
        "max_entities_per_request": 1,
    },
    "enrich": {
        "readiness_score": True,
        "field_gaps": True,
        "enrichment_targets": True,
        "auto_enrich": True,
        "batch_scan": False,
        "conversion_tracking": False,
        "max_entities_per_request": 10,
    },
    "discover": {
        "readiness_score": True,
        "field_gaps": True,
        "enrichment_targets": True,
        "auto_enrich": True,
        "batch_scan": True,
        "conversion_tracking": False,
        "max_entities_per_request": 100,
    },
    "autonomous": {
        "readiness_score": True,
        "field_gaps": True,
        "enrichment_targets": True,
        "auto_enrich": True,
        "batch_scan": True,
        "conversion_tracking": False,
        "max_entities_per_request": 1000,
    },
}

# The tier whose feature set is the engine's full health mechanism. Used when
# commercial mode is off: there are no tiers to restrict, so nothing is held back.
FULL_CAPABILITY_TIER: HealthTier = "autonomous"


class EffectiveHealthCapabilities(BaseModel):
    """What one health call may do — decided by the server, never by the request.

    HEALTH-02 / COMM-01 / COMM-02: ``TIER_FEATURES`` was keyed by the request's
    self-declared ``tier``, so any caller could claim ``autonomous`` and receive
    auto-enrichment (which spends EIE budget), while Seed callers were shown
    upgrade prompts and recorded in a conversion funnel for a product that has
    no billing or entitlement source. This is the single resolver every health
    subaction and the report generator consult.

    ``requested_tier`` is echoed for observability only; it grants nothing.
    """

    tier: HealthTier
    requested_tier: str | None = None
    commercial_mode: bool = False
    features: dict[str, Any] = Field(default_factory=dict)
    # Upgrade prompts and conversion events exist only inside a commercial model.
    upgrade_prompts: bool = False
    conversion_tracking: bool = False

    def allows(self, feature: str) -> bool:
        return bool(self.features.get(feature, False))


def resolve_effective_capabilities(requested_tier: str | None = None) -> EffectiveHealthCapabilities:
    """Resolve the capabilities a health call runs with.

    * ``health_commercial_mode_enabled`` off (default): the full capability set
      (``FULL_CAPABILITY_TIER``), no upgrade prompts, no conversion tracking.
      The request's ``tier`` is recorded and ignored.
    * On: the server-owned ``health_entitled_tier`` selects the ``TIER_FEATURES``
      row (an unknown value resolves to ``seed`` and is logged); upgrade prompts
      are active and conversion tracking follows that row. The request's ``tier``
      is still ignored — there is no path by which a request grants itself a tier.
    """
    # Imported here, not at module top, so tests that patch
    # ``engine.config.settings.settings`` are honoured (the handlers do the same).
    from engine.config.settings import settings

    if not settings.health_commercial_mode_enabled:
        features = dict(TIER_FEATURES[FULL_CAPABILITY_TIER])
        features["conversion_tracking"] = False
        return EffectiveHealthCapabilities(
            tier=FULL_CAPABILITY_TIER,
            requested_tier=requested_tier,
            commercial_mode=False,
            features=features,
            upgrade_prompts=False,
            conversion_tracking=False,
        )

    entitled = settings.health_entitled_tier
    if entitled not in TIER_FEATURES:
        logger.warning(
            "health_entitled_tier=%r is not a known tier; resolving to 'seed'",
            entitled,
        )
        entitled = "seed"
    features = dict(TIER_FEATURES[entitled])
    return EffectiveHealthCapabilities(
        tier=entitled,  # type: ignore[arg-type]
        requested_tier=requested_tier,
        commercial_mode=True,
        features=features,
        upgrade_prompts=True,
        conversion_tracking=bool(features.get("conversion_tracking", False)),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REQUEST SCHEMAS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class HealthAssessmentRequest(BaseModel):
    """Single entity health assessment request.

    ``tier`` is the caller's declaration. It is echoed back as
    ``requested_tier`` and never grants a capability (HEALTH-02).

    ``domain`` may be omitted: the domain a health call reads is the packet
    tenant (HEALTH-05, ``api.resolve_domain``), and a value naming anything
    else is rejected there.
    """

    entity_id: str
    domain: str | None = None
    node_label: str
    entity_data: dict[str, Any] = Field(default_factory=dict)
    field_confidences: dict[str, float] | None = None
    tier: HealthTier = "seed"


class BatchHealthRequest(BaseModel):
    """Batch health assessment request.

    BATCH-01: ``cost_ceiling_usd`` and ``max_entities`` are caller *requests*
    bounded above by the server-owned ceilings in ``Settings``
    (``health_batch_cost_ceiling_usd``, ``health_batch_max_entities``).
    A negative or non-numeric value is rejected at validation; ``None`` means
    "use the server ceiling".
    """

    domain: str | None = None
    tier: HealthTier = "enrich"
    scan_profile: HealthScanProfile | None = None
    cost_ceiling_usd: float | None = Field(default=None, ge=0)
    max_entities: int | None = Field(default=None, ge=0)
    entities: list[dict[str, Any]] = Field(default_factory=list)


class HealthScanProfile(BaseModel):
    """Profile for incremental batch scanning."""

    min_staleness_days: int = 30
    max_readiness_score: float = 70.0
    include_failed_matches: bool = True
    include_never_enriched: bool = True
    exclude_entity_ids: list[str] = Field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RESPONSE SCHEMAS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EntityHealthResponse(BaseModel):
    """Full health assessment response.

    ``tier`` is the *effective* tier the call ran with (server-resolved);
    ``requested_tier`` is what the caller declared. ``enrichment_dispatch`` is
    the full ``trigger_reenrichment_v2`` result when auto-enrichment was
    attempted (HEALTH-03), so a caller can see whether Gate accepted the request
    rather than inferring it from a priority recommendation.
    """

    entity_health: EntityHealth
    enrichment_priority: EnrichmentPriority | None = None
    match_quality_delta: MatchQualityDelta | None = None
    tier: str = "seed"
    requested_tier: str | None = None
    commercial_mode: bool = False
    upgrade_prompt: str | None = None
    enrichment_dispatch: dict[str, Any] | None = None


class HealthReport(BaseModel):
    """Structured health report for an entity."""

    entity_id: str
    domain: str
    grade: str  # A-F
    readiness_score: float
    gate_completeness: float
    scoring_dimension_coverage: float
    critical_gaps: list[str] = []
    enrichment_targets_count: int = 0
    recommended_actions: list[str] = []
    upgrade_recommendations: list[str] = []
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class BatchHealthReport(BaseModel):
    """Batch scan results.

    BATCH-02 accounting (every entity in the request lands in exactly one of
    the four buckets below):

    * ``total_scanned``      — assessed in this call;
    * ``estimated_eligible`` — assessed and recommended for enrichment
      (formerly ``total_enriched``, which overstated: no enrichment is sent
      by a batch scan);
    * ``total_skipped``      — assessed with recommendation ``skip``, plus
      entities inside the ``max_entities`` window left unassessed once the
      cost ceiling was reached;
    * ``total_truncated``    — beyond the ``max_entities`` window, never looked at.

    ``dispatched`` counts enrichment requests actually sent through Gate. The
    batch scan sends none, so it reports ``0`` until a dispatch path exists
    and produces evidence. ``cost_usd`` is the *estimated* cost of the eligible
    set, not spend.
    """

    domain: str
    tier: str = "seed"
    requested_tier: str | None = None
    total_candidates: int = 0
    total_scanned: int = 0
    estimated_eligible: int = 0
    dispatched: int = 0
    total_skipped: int = 0
    total_truncated: int = 0
    cost_usd: float = 0.0
    cost_ceiling_usd: float = 0.0
    max_entities: int = 0
    cost_ceiling_reached: bool = False
    entities: list[EntityHealthResponse] = []
    scan_profile: HealthScanProfile | None = None


class HealthAction(BaseModel):
    """A recommended action from health assessment."""

    action_type: Literal["enrich_field", "refresh_field", "upgrade_tier", "skip"]
    field_name: str | None = None
    reason: str = ""
    estimated_cost_tokens: int = 0
    priority: Literal["urgent", "high", "normal", "low"] = "normal"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONVERSION TRACKING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ConversionEvent(BaseModel):
    """Tracks Seed→Enrich conversion funnel events."""

    tenant: str
    entity_id: str
    event_type: Literal[
        "health_viewed",
        "gaps_shown",
        "upgrade_prompted",
        "upgrade_clicked",
        "upgrade_completed",
    ]
    tier_from: str = "seed"
    tier_to: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversionFunnelMetrics(BaseModel):
    """Aggregate conversion funnel metrics."""

    total_health_views: int = 0
    total_gaps_shown: int = 0
    total_upgrade_prompts: int = 0
    total_upgrade_clicks: int = 0
    total_upgrade_completions: int = 0
    view_to_prompt_rate: float = 0.0
    prompt_to_click_rate: float = 0.0
    click_to_complete_rate: float = 0.0
    overall_conversion_rate: float = 0.0
