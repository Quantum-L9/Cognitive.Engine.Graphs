"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, health, api, commercial, batch, tenancy]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_health_api.py
Unit tests for engine/health/api.py — the three health subactions.

Findings covered:
  HEALTH-02 / COMM-01 / COMM-02  request tier never grants; one capability resolver;
                                  upgrade prompts and conversion events only in
                                  commercial mode
  BATCH-01 / BATCH-02             server-owned caps; honest batch accounting
  HEALTH-03                       assess surfaces the trigger_reenrichment_v2 outcome;
                                  reason token == dispatch.error
  HEALTH-05                       payload.domain must equal the packet tenant
  HEALTH-01                       the canonical `health-assess` action dispatches the same
                                  three subactions with the same results as `admin`

The field analysis pipeline (analyzer, scorer, field map, gap prioritizer,
ROI priority) is replaced with deterministic fakes: these tests are about the
handlers' orchestration — what they grant, count, surface and refuse — not
about the scoring math, which tests/unit/test_health_engine.py owns.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from engine.config.settings import Settings
from engine.health import api as health_api
from engine.health import enrichment_trigger
from engine.health.field_health import EnrichmentPriority, EnrichmentTarget, FieldHealth, ReadinessScore
from engine.health.health_schemas import (
    FULL_CAPABILITY_TIER,
    TIER_FEATURES,
    resolve_effective_capabilities,
)

pytestmark = pytest.mark.unit

TENANT = "plasticos"


def _settings(**overrides: Any) -> Settings:
    return Settings(neo4j_password="test-pw", api_secret_key="test-key", **overrides)


def _fh(name: str, *, populated: bool, gate_critical: bool = False) -> FieldHealth:
    return FieldHealth(field_name=name, is_populated=populated, is_gate_critical=gate_critical, scoring_weight=0.1)


def _priority(recommendation: str, cost_usd: float = 0.02) -> EnrichmentPriority:
    return EnrichmentPriority(
        priority_score=90.0,
        estimated_cost_tokens=4000,
        estimated_cost_usd=cost_usd,
        expected_value_usd=50.0,
        roi=5.0,
        recommendation=recommendation,  # type: ignore[arg-type]
        reasoning="fake",
    )


class _Pipeline:
    """Deterministic stand-in for the analysis pipeline, with recorders."""

    def __init__(self) -> None:
        self.gaps: list[str] = ["capacity"]
        self.recommendations: list[str] = []  # consumed in order by compute_enrichment_priority
        self.default_recommendation = "enrich_now"
        self.cost_usd = 0.02
        self.events: list[tuple[str, str, str]] = []
        self.dispatch_calls: list[tuple[str, str]] = []
        self.dispatch_result: dict[str, Any] = {
            "triggered": False,
            "reason": "auto_enrich_via_gate_disabled",
            "recommendation": "enrich_now",
            "dispatch": {"status": "skipped", "error": "auto_enrich_via_gate_disabled"},
        }
        self.loaded_domains: list[str] = []

    # -- fakes -----------------------------------------------------------
    def load_domain(self, domain_id: str) -> Any:
        self.loaded_domains.append(domain_id)
        return SimpleNamespace(domain=SimpleNamespace(id=domain_id, description="test marketplace"))

    def analyze(self, *, entity: dict[str, Any], domain_spec: Any, node_label: str, **_: Any) -> list[FieldHealth]:
        return [_fh("name", populated=True), *(_fh(g, populated=False, gate_critical=True) for g in self.gaps)]

    def readiness(self, field_health: list[FieldHealth], domain_spec: Any) -> ReadinessScore:
        return ReadinessScore(overall_score=42.0, grade="D", gate_completeness=0.5, scoring_dimension_coverage=0.5)

    def field_map(self, domain_spec: Any, node_label: str) -> Any:
        return SimpleNamespace(inference_rules=[])

    def targets(self, field_health: list[FieldHealth], domain_spec: Any, *, inference_rules: Any) -> list[Any]:
        return [EnrichmentTarget(field_name=g, priority_score=700.0, is_gate_critical=True) for g in self.gaps]

    def priority(self, entity_health: Any, domain_spec: Any, *_: Any) -> EnrichmentPriority:
        rec = self.recommendations.pop(0) if self.recommendations else self.default_recommendation
        return _priority(rec, self.cost_usd)

    async def trigger(self, entity_health: Any, domain_spec: Any, tenant: str, *_: Any) -> dict[str, Any]:
        self.dispatch_calls.append((tenant, entity_health.entity_id))
        return dict(self.dispatch_result)

    def track(self, tenant: str, entity_id: str, event_type: str, **_: Any) -> Any:
        self.events.append((tenant, entity_id, event_type))
        return MagicMock()


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> _Pipeline:
    p = _Pipeline()
    loader = MagicMock()
    loader.load_domain.side_effect = p.load_domain
    monkeypatch.setattr(health_api, "_get_domain_loader", lambda: loader)
    monkeypatch.setattr(health_api, "analyze_entity_fields", p.analyze)
    monkeypatch.setattr(health_api, "compute_readiness_score_v2", p.readiness)
    monkeypatch.setattr(health_api, "build_field_map", p.field_map)
    monkeypatch.setattr(health_api, "prioritize_gaps_v2", p.targets)
    monkeypatch.setattr(health_api, "compute_enrichment_priority", p.priority)
    monkeypatch.setattr(health_api, "trigger_reenrichment_v2", p.trigger)
    monkeypatch.setattr(health_api, "track_conversion_event", p.track)
    return p


@pytest.fixture
def settings_off() -> Iterator[Settings]:
    s = _settings()
    with patch("engine.config.settings.settings", s):
        yield s


@pytest.fixture
def settings_commercial_seed() -> Iterator[Settings]:
    s = _settings(health_commercial_mode_enabled=True, health_entitled_tier="seed")
    with patch("engine.config.settings.settings", s):
        yield s


def _assess_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entity_id": "ent-1",
        "domain": TENANT,
        "node_label": "Facility",
        "entity_data": {"name": "Acme"},
    }
    payload.update(overrides)
    return payload


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — HEALTH-02 / COMM-01 / COMM-02
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.finding("HEALTH-02")
class TestCapabilityResolver:
    def test_commercial_off_grants_full_capabilities_and_no_funnel(self, settings_off: Settings) -> None:
        caps = resolve_effective_capabilities("seed")
        assert caps.tier == FULL_CAPABILITY_TIER
        assert caps.requested_tier == "seed"
        assert caps.commercial_mode is False
        assert caps.upgrade_prompts is False
        assert caps.conversion_tracking is False
        for feature in ("enrichment_targets", "auto_enrich", "batch_scan"):
            assert caps.allows(feature), feature
        assert caps.features["conversion_tracking"] is False

    @pytest.mark.parametrize("requested", ["seed", "enrich", "discover", "autonomous", None, "platinum"])
    def test_request_tier_never_changes_the_outcome(self, settings_off: Settings, requested: str | None) -> None:
        caps = resolve_effective_capabilities(requested)
        assert caps.tier == FULL_CAPABILITY_TIER
        assert caps.features == {**TIER_FEATURES[FULL_CAPABILITY_TIER], "conversion_tracking": False}

    def test_commercial_on_uses_server_entitlement_not_request(self, settings_commercial_seed: Settings) -> None:
        caps = resolve_effective_capabilities("autonomous")
        assert caps.tier == "seed"
        assert caps.requested_tier == "autonomous"
        assert caps.commercial_mode is True
        assert caps.upgrade_prompts is True
        assert caps.conversion_tracking is True
        assert caps.allows("auto_enrich") is False
        assert caps.allows("batch_scan") is False

    def test_unknown_entitled_tier_resolves_to_seed(self) -> None:
        s = _settings(health_commercial_mode_enabled=True, health_entitled_tier="platinum")
        with patch("engine.config.settings.settings", s):
            caps = resolve_effective_capabilities("autonomous")
        assert caps.tier == "seed"

    def test_tier_enum_and_table_are_retained(self) -> None:
        assert set(TIER_FEATURES) == {"seed", "enrich", "discover", "autonomous"}


@pytest.mark.finding("COMM-01")
class TestAssessTierGrantsNothing:
    async def test_seed_request_gets_full_capabilities_when_commercial_off(
        self, pipeline: _Pipeline, settings_off: Settings
    ) -> None:
        result = await health_api.handle_health_assess(TENANT, _assess_payload(tier="seed"))
        assert result["tier"] == FULL_CAPABILITY_TIER
        assert result["requested_tier"] == "seed"
        assert result["commercial_mode"] is False
        # Full capability set: targets, priority and the auto-enrich attempt all happen.
        assert [t["field_name"] for t in result["entity_health"]["enrichment_targets"]] == ["capacity"]
        assert result["enrichment_priority"]["recommendation"] == "enrich_now"
        assert pipeline.dispatch_calls == [(TENANT, "ent-1")]

    async def test_autonomous_request_gets_seed_when_server_entitles_seed(
        self, pipeline: _Pipeline, settings_commercial_seed: Settings
    ) -> None:
        result = await health_api.handle_health_assess(TENANT, _assess_payload(tier="autonomous"))
        assert result["tier"] == "seed"
        assert result["requested_tier"] == "autonomous"
        assert result["entity_health"]["enrichment_targets"] == []
        assert result["enrichment_priority"] is None
        assert result["enrichment_dispatch"] is None
        assert pipeline.dispatch_calls == [], "a self-declared tier must not buy an EIE dispatch"


@pytest.mark.finding("COMM-02")
class TestFunnelSuppressedOutsideCommercialMode:
    async def test_assess_off_no_prompt_no_events_no_upsell_action(
        self, pipeline: _Pipeline, settings_off: Settings
    ) -> None:
        result = await health_api.handle_health_assess(TENANT, _assess_payload(tier="seed"))
        assert result["upgrade_prompt"] is None
        assert pipeline.events == []
        assert not any("Upgrade" in a for a in result["entity_health"]["recommended_actions"])

    async def test_assess_on_seed_prompts_and_tracks(
        self, pipeline: _Pipeline, settings_commercial_seed: Settings
    ) -> None:
        result = await health_api.handle_health_assess(TENANT, _assess_payload(tier="seed"))
        assert result["upgrade_prompt"] is not None
        assert "Upgrade to Enrich" in result["upgrade_prompt"]
        assert [e[2] for e in pipeline.events] == ["health_viewed", "gaps_shown", "upgrade_prompted"]
        assert any("Upgrade to Enrich tier" in a for a in result["entity_health"]["recommended_actions"])

    async def test_report_off_has_no_upgrade_recommendations(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        result = await health_api.handle_health_report(TENANT, {"entity_id": "ent-1", "tier": "seed"})
        assert result["upgrade_recommendations"] == []
        assert result["critical_gaps"] == ["capacity"]
        assert pipeline.events == []

    async def test_report_on_seed_recommends_and_tracks(
        self, pipeline: _Pipeline, settings_commercial_seed: Settings
    ) -> None:
        result = await health_api.handle_health_report(TENANT, {"entity_id": "ent-1", "tier": "seed"})
        assert result["upgrade_recommendations"], "commercial mode on: the upsell is the product"
        assert [e[2] for e in pipeline.events] == ["health_viewed", "gaps_shown", "upgrade_prompted"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H2 — BATCH-01 / BATCH-02
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _entities(n: int) -> list[dict[str, Any]]:
    return [{"entity_id": f"ent-{i}", "node_label": "Facility", "data": {}} for i in range(n)]


@pytest.mark.finding("BATCH-01")
class TestBatchServerOwnedCaps:
    async def test_request_cannot_raise_server_ceilings(self, pipeline: _Pipeline) -> None:
        s = _settings(health_batch_max_entities=2, health_batch_cost_ceiling_usd=1.0)
        with patch("engine.config.settings.settings", s):
            result = await health_api.handle_health_batch_assess(
                TENANT, {"entities": _entities(5), "max_entities": 50, "cost_ceiling_usd": 999.0}
            )
        assert result["max_entities"] == 2
        assert result["cost_ceiling_usd"] == 1.0
        assert result["total_scanned"] == 2
        assert result["total_truncated"] == 3

    async def test_request_may_lower_server_ceilings(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        result = await health_api.handle_health_batch_assess(
            TENANT, {"entities": _entities(5), "max_entities": 1, "cost_ceiling_usd": 0.5}
        )
        assert result["max_entities"] == 1
        assert result["cost_ceiling_usd"] == 0.5
        assert result["total_scanned"] == 1

    async def test_omitted_caps_use_server_defaults(self, pipeline: _Pipeline) -> None:
        s = _settings(health_batch_max_entities=7, health_batch_cost_ceiling_usd=3.5)
        with patch("engine.config.settings.settings", s):
            result = await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(2)})
        assert result["max_entities"] == 7
        assert result["cost_ceiling_usd"] == 3.5

    @pytest.mark.parametrize(
        "bad",
        [
            {"max_entities": -1},
            {"cost_ceiling_usd": -0.01},
            {"max_entities": "ten"},
            {"cost_ceiling_usd": "cheap"},
            {"max_entities": 2.5},
        ],
    )
    async def test_negative_or_non_numeric_caps_are_rejected(
        self, pipeline: _Pipeline, settings_off: Settings, bad: dict[str, Any]
    ) -> None:
        # pydantic's ValidationError is a ValueError, which the chassis maps to 422.
        with pytest.raises(ValueError):
            await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(1), **bad})
        assert pipeline.loaded_domains == [], "rejected before any domain pack is loaded"


@pytest.mark.finding("BATCH-02")
class TestBatchAccounting:
    async def test_buckets_partition_the_request(self, pipeline: _Pipeline) -> None:
        # window of 3 out of 4; first is eligible at 0.02 which meets the 0.02 ceiling,
        # so the second iteration stops: 1 scanned, 2 unassessed-in-window, 1 truncated.
        pipeline.recommendations = ["enrich_now", "skip", "enrich_now"]
        s = _settings(health_batch_max_entities=3, health_batch_cost_ceiling_usd=0.02)
        with patch("engine.config.settings.settings", s):
            result = await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(4)})
        assert result["total_candidates"] == 4
        assert result["total_scanned"] == 1
        assert result["estimated_eligible"] == 1
        assert result["total_skipped"] == 2
        assert result["total_truncated"] == 1
        assert result["cost_ceiling_reached"] is True
        assert result["total_scanned"] + result["total_skipped"] + result["total_truncated"] == 4
        assert "total_enriched" not in result

    async def test_ceiling_is_checked_before_an_entity_is_admitted(self, pipeline: _Pipeline) -> None:
        # Two eligible entities at 0.02 each against a 0.03 ceiling: the second
        # would take the batch to 0.04, so it is never admitted and cost_usd
        # stays within the server-owned ceiling (it used to report 0.04).
        pipeline.cost_usd = 0.02
        s = _settings(health_batch_max_entities=3, health_batch_cost_ceiling_usd=0.03)
        with patch("engine.config.settings.settings", s):
            result = await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(3)})
        assert result["total_scanned"] == 1
        assert result["estimated_eligible"] == 1
        assert result["total_skipped"] == 2
        assert result["cost_usd"] == pytest.approx(0.02)
        assert result["cost_usd"] <= result["cost_ceiling_usd"]
        assert result["cost_ceiling_reached"] is True

    async def test_skip_recommendations_count_once(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        pipeline.recommendations = ["skip", "enrich_low_priority", "skip"]
        result = await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(3)})
        assert result["total_scanned"] == 3
        assert result["estimated_eligible"] == 1
        assert result["total_skipped"] == 2
        assert result["total_truncated"] == 0
        assert result["cost_ceiling_reached"] is False

    async def test_dispatched_is_zero_without_dispatch_evidence(
        self, pipeline: _Pipeline, settings_off: Settings
    ) -> None:
        result = await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(2)})
        assert result["dispatched"] == 0
        assert pipeline.dispatch_calls == [], "batch scan sends nothing through Gate"
        assert result["estimated_eligible"] == 2

    async def test_batch_scan_refused_when_effective_tier_lacks_it(
        self, pipeline: _Pipeline, settings_commercial_seed: Settings
    ) -> None:
        with pytest.raises(ValueError, match="Batch scan is not available"):
            await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(1), "tier": "autonomous"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H3 — HEALTH-03
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.finding("HEALTH-03")
class TestAssessSurfacesDispatchOutcome:
    async def test_dispatch_result_is_returned_verbatim(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        result = await health_api.handle_health_assess(TENANT, _assess_payload())
        assert result["enrichment_dispatch"] == pipeline.dispatch_result
        assert result["enrichment_dispatch"]["reason"] == result["enrichment_dispatch"]["dispatch"]["error"]

    async def test_no_dispatch_when_not_recommended(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        pipeline.default_recommendation = "enrich_low_priority"
        result = await health_api.handle_health_assess(TENANT, _assess_payload())
        assert result["enrichment_dispatch"] is None
        assert pipeline.dispatch_calls == []

    async def test_trigger_reason_token_matches_dispatch_error_when_gate_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(enrichment_trigger.settings, "auto_enrich_via_gate", False)
        monkeypatch.setattr(enrichment_trigger, "compute_enrichment_priority", lambda *_: _priority("enrich_now"))
        health = SimpleNamespace(entity_id="ent-1", domain=TENANT, enrichment_targets=[], critical_gaps=["capacity"])
        result = await enrichment_trigger.trigger_reenrichment_v2(health, SimpleNamespace(), TENANT)  # type: ignore[arg-type]
        assert result["triggered"] is False
        assert result["reason"] == "auto_enrich_via_gate_disabled"
        assert result["reason"] == result["dispatch"]["error"]

    async def test_trigger_reason_token_matches_dispatch_error_when_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(enrichment_trigger, "compute_enrichment_priority", lambda *_: _priority("skip"))
        health = SimpleNamespace(entity_id="ent-1", domain=TENANT, enrichment_targets=[], critical_gaps=[])
        result = await enrichment_trigger.trigger_reenrichment_v2(health, SimpleNamespace(), TENANT)  # type: ignore[arg-type]
        assert result["triggered"] is False
        assert result["reason"] == "enrichment_not_recommended"
        assert result["reason"] == result["dispatch"]["error"]
        assert result["priority"]["reasoning"] == "fake", "the human ROI breakdown stays on priority"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H4 — HEALTH-05
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.finding("HEALTH-05")
class TestDomainMustEqualTenant:
    async def test_assess_rejects_foreign_domain(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        with pytest.raises(ValueError, match="does not match the packet tenant"):
            await health_api.handle_health_assess(TENANT, _assess_payload(domain="other-tenant"))
        assert pipeline.loaded_domains == []

    async def test_batch_rejects_foreign_domain(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        with pytest.raises(ValueError, match="does not match the packet tenant"):
            await health_api.handle_health_batch_assess(TENANT, {"domain": "other-tenant", "entities": _entities(1)})
        assert pipeline.loaded_domains == []

    async def test_report_rejects_foreign_domain(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        with pytest.raises(ValueError, match="does not match the packet tenant"):
            await health_api.handle_health_report(TENANT, {"entity_id": "ent-1", "domain": "other-tenant"})
        assert pipeline.loaded_domains == []

    async def test_absent_domain_resolves_to_tenant(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        await health_api.handle_health_batch_assess(TENANT, {"entities": _entities(1)})
        await health_api.handle_health_report(TENANT, {"entity_id": "ent-1"})
        assert pipeline.loaded_domains == [TENANT, TENANT]

    async def test_assess_without_domain_resolves_to_tenant(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        payload = _assess_payload()
        del payload["domain"]
        result = await health_api.handle_health_assess(TENANT, payload)
        assert result["entity_health"]["domain"] == TENANT
        assert pipeline.loaded_domains == [TENANT]

    async def test_matching_domain_is_accepted(self, pipeline: _Pipeline, settings_off: Settings) -> None:
        result = await health_api.handle_health_assess(TENANT, _assess_payload(domain=TENANT))
        assert result["entity_health"]["domain"] == TENANT

    async def test_handle_admin_rejects_before_dispatch(
        self, pipeline: _Pipeline, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from engine.handlers import ValidationError, handle_admin, init_dependencies
        from engine.state import _reset_singleton

        monkeypatch.delenv("TENANT_ALLOWLIST", raising=False)
        _reset_singleton()
        init_dependencies(MagicMock(), MagicMock())

        async def _must_not_run(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("health handler must not be reached for a foreign domain")

        monkeypatch.setattr(health_api, "handle_health_assess", _must_not_run)
        monkeypatch.setattr(health_api, "handle_health_batch_assess", _must_not_run)
        monkeypatch.setattr(health_api, "handle_health_report", _must_not_run)
        try:
            with patch("engine.config.settings.settings", _settings(health_api_enabled=True)):
                for subaction in ("health_assess", "health_batch_assess", "health_report"):
                    with pytest.raises(ValidationError, match="does not match packet tenant"):
                        await handle_admin(TENANT, {"subaction": subaction, "domain": "other-tenant"})
        finally:
            _reset_singleton()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H9 — HEALTH-01: canonical `health-assess` action
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def engine_up(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An initialised engine with no tenant allowlist (CEG-AUTH-01 admits all outside prod)."""
    from engine.handlers import init_dependencies
    from engine.state import _reset_singleton

    monkeypatch.delenv("TENANT_ALLOWLIST", raising=False)
    _reset_singleton()
    init_dependencies(MagicMock(), MagicMock())
    try:
        yield
    finally:
        _reset_singleton()


def _health_payload(subaction: str) -> dict[str, Any]:
    if subaction == "health_assess":
        return _assess_payload(subaction=subaction)
    if subaction == "health_batch_assess":
        return {"subaction": subaction, "entities": _entities(2)}
    return {"subaction": subaction, "entity_id": "ent-1"}


@pytest.mark.finding("HEALTH-01")
class TestHealthAssessAction:
    def test_registered_as_its_own_action(self) -> None:
        from engine.handlers import ACTION_HANDLERS, handle_admin, handle_health_assess

        assert ACTION_HANDLERS["health-assess"] is handle_health_assess
        assert ACTION_HANDLERS["health-assess"] is not handle_admin

    @pytest.mark.parametrize("subaction", ["health_assess", "health_batch_assess", "health_report"])
    async def test_same_result_as_admin(self, subaction: str, pipeline: _Pipeline, engine_up: None) -> None:
        """One dispatcher: the Gate route and the operator route return identical bodies."""
        from engine.handlers import handle_admin, handle_health_assess

        with patch("engine.config.settings.settings", _settings(health_api_enabled=True)):
            via_action = await handle_health_assess(TENANT, _health_payload(subaction))
            via_admin = await handle_admin(TENANT, _health_payload(subaction))
        # health_report stamps `generated_at` from the wall clock on every call;
        # it is the one field two sequential calls legitimately differ on.
        if subaction == "health_report":
            action_generated_at = via_action.pop("generated_at", None)
            admin_generated_at = via_admin.pop("generated_at", None)
            assert action_generated_at
            assert admin_generated_at
        assert via_action.get("status") != "disabled"
        # RU-014 / CEG-COR-075 (C-23): the admin route stamps its response
        # envelope (`subaction`, and `status` where the body set none); the body
        # the shared dispatcher produced is otherwise identical.
        admin_subaction = via_admin.pop("subaction")
        admin_status = via_admin.pop("status")
        action_status = via_action.pop("status", "success")
        assert admin_subaction == subaction
        assert admin_status == action_status
        assert via_action == via_admin
        assert pipeline.loaded_domains == [TENANT, TENANT]

    @pytest.mark.parametrize("subaction", ["health_assess", "health_batch_assess", "health_report"])
    async def test_disabled_flag_reads_disabled_like_admin(
        self, subaction: str, pipeline: _Pipeline, engine_up: None
    ) -> None:
        from engine.handlers import handle_admin, handle_health_assess

        with patch("engine.config.settings.settings", _settings(health_api_enabled=False)):
            via_action = await handle_health_assess(TENANT, _health_payload(subaction))
            via_admin = await handle_admin(TENANT, _health_payload(subaction))
        assert via_action == via_admin
        assert via_action["status"] == "disabled"
        assert via_action["subaction"] == subaction
        assert pipeline.loaded_domains == []

    async def test_refuses_non_health_subaction_before_any_work(self, pipeline: _Pipeline, engine_up: None) -> None:
        from engine.handlers import ValidationError, handle_health_assess

        with patch("engine.config.settings.settings", _settings(health_api_enabled=True)):
            with pytest.raises(ValidationError, match="is not a health subaction") as exc_info:
                await handle_health_assess(TENANT, {"subaction": "list_domains"})
        assert exc_info.value.action == "health-assess"
        assert pipeline.loaded_domains == []

    async def test_missing_subaction_is_refused(self, pipeline: _Pipeline, engine_up: None) -> None:
        from engine.handlers import ValidationError, handle_health_assess

        with pytest.raises(ValidationError, match="Missing required field 'subaction' in health-assess payload"):
            await handle_health_assess(TENANT, _assess_payload())
        assert pipeline.loaded_domains == []

    async def test_foreign_domain_rejected_before_dispatch(
        self, pipeline: _Pipeline, engine_up: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HEALTH-05 holds on the canonical route exactly as on admin."""
        from engine.handlers import ValidationError, handle_health_assess

        async def _must_not_run(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("health handler must not be reached for a foreign domain")

        monkeypatch.setattr(health_api, "handle_health_assess", _must_not_run)
        monkeypatch.setattr(health_api, "handle_health_batch_assess", _must_not_run)
        monkeypatch.setattr(health_api, "handle_health_report", _must_not_run)
        with patch("engine.config.settings.settings", _settings(health_api_enabled=True)):
            for subaction in ("health_assess", "health_batch_assess", "health_report"):
                with pytest.raises(ValidationError, match="does not match packet tenant") as exc_info:
                    await handle_health_assess(TENANT, {"subaction": subaction, "domain": "other-tenant"})
                assert exc_info.value.action == "health-assess"

    async def test_tenant_allowlist_applies(self, pipeline: _Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:
        """CEG-AUTH-01: the canonical route honours TENANT_ALLOWLIST like every other action."""
        from engine.handlers import ValidationError, handle_health_assess, init_dependencies
        from engine.state import _reset_singleton

        monkeypatch.setenv("TENANT_ALLOWLIST", "someone-else")
        _reset_singleton()
        init_dependencies(MagicMock(), MagicMock())
        try:
            with patch("engine.config.settings.settings", _settings(health_api_enabled=True)):
                with pytest.raises(ValidationError, match="not in authorized allowlist"):
                    await handle_health_assess(TENANT, _health_payload("health_assess"))
        finally:
            _reset_singleton()
        assert pipeline.loaded_domains == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RU-108 / CEG-COR-044 — one readiness on every health path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_FACILITY = {
    "facility_id": "fac-1",
    "name": "Acme Recycling",
    "lat": 41.88,
    "lon": -87.63,
    "credit_score": 720,
    "min_density": 0.90,
    "max_density": 0.97,
    "min_mfi": 1.0,
    "max_mfi": 12.0,
    "contamination_tolerance": 0.05,
    "food_grade_certified": True,
    "capacity_tons_month": 400,
    "tier": "gold",
    "community_id": 3,
    "rate_per_ton": 180.0,
    "rate_confidence": 0.8,
    "last_transaction_date": "2026-09-01",
}


@pytest.fixture
def real_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real analysis pipeline over the checked-in plasticos pack; only the loader and dispatch are fakes."""
    from engine.config.loader import DomainPackLoader

    spec = DomainPackLoader().load_domain(TENANT)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    monkeypatch.setattr(health_api, "_get_domain_loader", lambda: loader)

    async def _no_dispatch(*_: Any, **__: Any) -> dict[str, Any]:
        return {"triggered": False, "reason": "test", "dispatch": {"status": "skipped", "error": "test"}}

    monkeypatch.setattr(health_api, "trigger_reenrichment_v2", _no_dispatch)


async def _three_readings(confidences: dict[str, float] | None) -> list[tuple[float, str, float]]:
    assess_payload: dict[str, Any] = {
        "entity_id": "fac-1",
        "node_label": "Facility",
        "entity_data": dict(_FACILITY),
    }
    batch_entity: dict[str, Any] = {"entity_id": "fac-1", "node_label": "Facility", "data": dict(_FACILITY)}
    report_payload: dict[str, Any] = {"entity_id": "fac-1", "node_label": "Facility", "entity_data": dict(_FACILITY)}
    if confidences is not None:
        assess_payload["field_confidences"] = confidences
        batch_entity["field_confidences"] = confidences
        report_payload["field_confidences"] = confidences

    assess = (await health_api.handle_health_assess(TENANT, assess_payload))["entity_health"]
    batch = (await health_api.handle_health_batch_assess(TENANT, {"entities": [batch_entity]}))["entities"][0][
        "entity_health"
    ]
    report = await health_api.handle_health_report(TENANT, report_payload)  # HealthReport is flat
    return [(h["readiness_score"], h["grade"], h["gate_completeness"]) for h in (assess, batch, report)]


@pytest.mark.finding("CEG-COR-044")
class TestReadinessAgreesAcrossSubactions:
    async def test_no_confidences_same_nonzero_readiness_everywhere(self, real_pipeline: None) -> None:
        with patch("engine.config.settings.settings", _settings()):
            readings = await _three_readings(None)
        assert len(set(readings)) == 1, readings
        score, grade, gate_completeness = readings[0]
        assert score > 0.0
        assert grade != "F"
        assert gate_completeness == 1.0

    async def test_supplied_confidences_forwarded_by_batch_and_report(self, real_pipeline: None) -> None:
        confidences = dict.fromkeys(_FACILITY, 0.95)
        confidences["contamination_tolerance"] = 0.5  # explicit low confidence on a gate field still discounts it
        with patch("engine.config.settings.settings", _settings()):
            readings = await _three_readings(confidences)
            baseline = await _three_readings(dict.fromkeys(_FACILITY, 0.95))
        assert len(set(readings)) == 1, readings
        assert readings[0][2] < 1.0
        assert readings[0][2] < baseline[0][2]

    async def test_flag_off_restores_legacy_outputs(self, real_pipeline: None) -> None:
        confidences = dict.fromkeys(_FACILITY, 0.95)
        with patch("engine.config.settings.settings", _settings(health_absent_confidence_counts_enabled=False)):
            without = await _three_readings(None)
            with_conf = await _three_readings(confidences)
        # Legacy: absent confidence coerces to 0 everywhere, and batch/report ignore a supplied map.
        assert all(r[0] == 0.0 and r[1] == "F" for r in without), without
        assert with_conf[0][0] > 0.0
        assert with_conf[1] == without[1]
        assert with_conf[2] == without[2]
