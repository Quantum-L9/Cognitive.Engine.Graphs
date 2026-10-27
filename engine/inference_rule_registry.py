"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [inference, registry]
owner: engine-team
status: active
--- /L9_META ---

GAP-3 FIX: Active inference function registry with real computation functions.

Previously _RULE_REGISTRY was empty at startup, causing every `derived_from`
inference rule that referenced a named rule to block with NO_RULE.
This module registers the supported in-code inference functions and exposes
them through the explicit execute_rule() registry boundary.

Registration pattern:
    @register_inference_rule("rule_name")
    def my_rule(entity: dict, context: InferenceContext) -> InferenceResult:
        ...

The inference engine calls: _RULE_REGISTRY[rule_name](entity, context)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from engine.config.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class InferenceContext:
    """Runtime context passed to every inference function."""

    tenant_id: str
    domain_id: str
    pass_number: int
    known_fields: dict[str, Any] = field(default_factory=dict)
    # Optional caller-supplied tuning context. DomainSpec/DomainPackLoader do not populate it.
    domain_kb: dict[str, Any] = field(default_factory=dict)
    confidence_floor: float = 0.55


@dataclass
class InferenceResult:
    """Output of a single inference function execution."""

    field_name: str
    value: Any
    confidence: float  # 0.0-1.0
    rule_name: str
    provenance: str = "inference"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "value": self.value,
            "confidence": self.confidence,
            "rule": self.rule_name,
            "provenance": self.provenance,
            "rationale": self.rationale,
        }


InferenceFn = Callable[[dict[str, Any], InferenceContext], InferenceResult | None]

# ---------------------------------------------------------------------------
# Registry — populated by @register_inference_rule decorators below
# ---------------------------------------------------------------------------

_RULE_REGISTRY: dict[str, InferenceFn] = {}


def register_inference_rule(name: str) -> Callable[[InferenceFn], InferenceFn]:
    """Decorator that registers an inference function under `name`."""

    def decorator(fn: InferenceFn) -> InferenceFn:
        if name in _RULE_REGISTRY:
            raise ValueError(f"Inference rule '{name}' is already registered")
        _RULE_REGISTRY[name] = fn
        logger.debug("Registered inference rule: %s → %s", name, fn.__qualname__)
        return fn

    return decorator


def get_rule(name: str) -> InferenceFn:
    """Return a registered rule or raise KeyError with a clear message."""
    if name not in _RULE_REGISTRY:
        available = sorted(_RULE_REGISTRY.keys())
        raise KeyError(f"Inference rule '{name}' not found in registry. Available rules: {available}")
    return _RULE_REGISTRY[name]


def execute_rule(
    rule_name: str,
    entity: dict[str, Any],
    context: InferenceContext,
) -> InferenceResult | None:
    """
    Execute a registered inference rule. Returns None if the rule
    returns None (no confident inference). Raises KeyError if rule
    is not registered (Gap-3: no silent NO_RULE block).
    """
    fn = get_rule(rule_name)
    try:
        result = fn(entity, context)
    except Exception:
        logger.exception("Inference rule '%s' raised during execution", rule_name)
        return None
    if result is not None and result.confidence < context.confidence_floor:
        logger.debug(
            "Rule '%s' produced confidence %.3f below floor %.3f — suppressing",
            rule_name,
            result.confidence,
            context.confidence_floor,
        )
        return None
    return result


# ===========================================================================
# GENERIC INFERENCE RULES
# Register domain-neutral in-code inference rules here. Domain configuration
# remains owned by the typed DomainSpec/DomainPackLoader boundary; raw rule
# dictionaries are not loaded into this registry.
#
# Domain neutrality (Stage 2, R02): this registry carries no business-vertical
# rule. The former plastics rules (facility tier from processing capacity,
# material grade from melt-flow index, contamination tolerance) were PlasticOS
# semantics that CEG does not own; they were removed, not relocated, because no
# authorized domain mechanism supplies executable rules to CEG yet. A domain
# authorizes which registered rules may run for it through
# ``DomainSpec.plugins.inference_rules`` (see ``authorized_rules``); the admin
# ``emit_inference_feedback`` pathway executes nothing a domain did not declare.
# ===========================================================================


def authorized_rules(domain_spec: Any) -> list[str]:
    """Rule names the domain spec authorizes for inference-feedback execution.

    Reads ``DomainSpec.plugins.inference_rules`` — the existing plugin extension
    point — and returns an empty list when the domain declares nothing. The
    generic engine never substitutes "every registered rule" for an absent
    declaration.
    """
    plugins = getattr(domain_spec, "plugins", None)
    declared = getattr(plugins, "inference_rules", None) if plugins is not None else None
    return [str(name) for name in (declared or [])]


@register_inference_rule("infer_company_size_tier")
def infer_company_size_tier(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer company_size_tier from employee_count and revenue fields."""
    emp = entity.get("employee_count") or entity.get("employees")
    revenue = entity.get("annual_revenue_usd") or entity.get("revenue")
    if emp is None and revenue is None:
        return None
    try:
        emp_n = int(emp) if emp is not None else None
        rev_n = float(revenue) if revenue is not None else None
    except (TypeError, ValueError):
        return None
    if emp_n is not None:
        if emp_n < 10:
            tier, conf = "micro", 0.90
        elif emp_n < 50:
            tier, conf = "small", 0.88
        elif emp_n < 250:
            tier, conf = "mid_market", 0.85
        elif emp_n < 1000:
            tier, conf = "enterprise", 0.83
        else:
            tier, conf = "large_enterprise", 0.88
        return InferenceResult(
            field_name="company_size_tier",
            value=tier,
            confidence=conf,
            rule_name="infer_company_size_tier",
            rationale=f"employee_count={emp_n}",
        )
    # fallback: revenue-only
    if rev_n is not None:
        if rev_n < 1_000_000:
            tier, conf = "micro", 0.70
        elif rev_n < 10_000_000:
            tier, conf = "small", 0.68
        elif rev_n < 100_000_000:
            tier, conf = "mid_market", 0.65
        else:
            tier, conf = "enterprise", 0.65
        return InferenceResult(
            field_name="company_size_tier",
            value=tier,
            confidence=conf,
            rule_name="infer_company_size_tier",
            rationale=f"annual_revenue={rev_n} (fallback)",
        )
    return None


@register_inference_rule("infer_email_domain_from_website")
def infer_email_domain_from_website(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer corporate email domain from website URL."""
    website = entity.get("website") or entity.get("website_url")
    if not website:
        return None
    # Strip protocol and www
    domain = re.sub(r"^https?://", "", str(website).strip().lower())
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split("/")[0].split("?")[0]
    if not domain or "." not in domain:
        return None
    return InferenceResult(
        field_name="email_domain",
        value=domain,
        confidence=0.92,
        rule_name="infer_email_domain_from_website",
        rationale=f"derived from website={website}",
    )


# USPS ZIP first digit -> region, in the vocabulary this rule already emits
# (RU-084). Zone-level and therefore approximate (zone 5 includes MT).
_USPS_ZONE_REGION: dict[str, str] = {
    "0": "Northeast",
    "1": "Northeast",
    "2": "Southeast",
    "3": "Southeast",
    "4": "Midwest",
    "5": "Midwest",
    "6": "Midwest",
    "7": "South",
    "8": "West",
    "9": "West",
}
# Declared-country spellings that keep the US ZIP inference (compared
# case-insensitively after stripping whitespace).
_US_COUNTRY_DESIGNATIONS = frozenset({"us", "usa", "united states"})


@register_inference_rule("infer_geography_from_postal_code")
def infer_geography_from_postal_code(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer region/country from postal code prefix.

    With ``inference_geo_usps_zones_enabled`` the region follows the USPS
    first-digit zone, and an entity whose declared ``country`` is not a US
    designation yields no inference (an absent country keeps the inference,
    R4-DEC-07). Off: the legacy five-digit threshold ladder.
    """
    postal = entity.get("postal_code") or entity.get("zip_code")
    if not postal:
        return None
    postal_str = str(postal).strip().upper()
    if settings.inference_geo_usps_zones_enabled:
        country = entity.get("country")
        if country is not None and str(country).strip().lower() not in _US_COUNTRY_DESIGNATIONS:
            return None
        if not re.match(r"^\d{5}(-\d{4})?$", postal_str):
            return None
        return InferenceResult(
            field_name="region",
            value=_USPS_ZONE_REGION[postal_str[0]],
            confidence=0.82,
            rule_name="infer_geography_from_postal_code",
            rationale=f"US zip={postal_str}",
        )
    # US zip
    if re.match(r"^\d{5}(-\d{4})?$", postal_str):
        prefix = int(postal_str[:5])
        if prefix < 20000:
            region = "Northeast"
        elif prefix < 30000:
            region = "Southeast"
        elif prefix < 50000:
            region = "Midwest"
        elif prefix < 80000:
            region = "South"
        else:
            region = "West"
        return InferenceResult(
            field_name="region",
            value=region,
            confidence=0.82,
            rule_name="infer_geography_from_postal_code",
            rationale=f"US zip={postal_str}",
        )
    return None


@register_inference_rule("infer_icp_fit_score")
def infer_icp_fit_score(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """
    Generic ICP fit scoring: weighted sum of known firmographic signals.
    Returns a 0.0-1.0 score in the 'icp_fit_score' field.
    """
    score = 0.0
    factors = 0
    if entity.get("company_size_tier") in ("mid_market", "enterprise", "large_enterprise"):
        score += 0.30
        factors += 1
    rev = entity.get("annual_revenue_usd")
    if rev:
        try:
            if float(rev) > 1_000_000:
                score += 0.25
                factors += 1
        except (TypeError, ValueError):
            # Non-numeric / malformed revenue value: skip this factor rather
            # than fail the whole scoring pass.
            pass
    if entity.get("email_domain"):
        score += 0.10
        factors += 1
    if entity.get("region"):
        score += 0.10
        factors += 1
    if factors == 0:
        return None
    # Confidence scales with number of contributing factors
    conf = min(0.55 + (factors * 0.06), 0.90)
    return InferenceResult(
        field_name="icp_fit_score",
        value=round(score, 4),
        confidence=conf,
        rule_name="infer_icp_fit_score",
        rationale=f"{factors} contributing factors",
    )


# Job-title tokenizer for infer_buyer_persona (RU-088): '&' is kept so the
# declared 'r&d' keyword remains matchable.
_TITLE_TOKEN_SPLIT = re.compile(r"[^a-z0-9&]+")


@register_inference_rule("infer_buyer_persona")
def infer_buyer_persona(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer buyer_persona from job_title if present.

    With ``inference_persona_token_match_enabled`` a keyword matches only a
    whole token of the title, split on ``[^a-z0-9&]+`` so ``r&d`` stays one
    token ('Marketing Coordinator' no longer matches 'coo'). Off: the legacy
    substring test. Keyword sets, precedence and confidences are unchanged.
    """
    title = entity.get("job_title") or entity.get("title") or ""
    title_lower = str(title).lower()
    if not title_lower:
        return None
    if settings.inference_persona_token_match_enabled:
        tokens = frozenset(_TITLE_TOKEN_SPLIT.split(title_lower))

        def _has(kw: str) -> bool:
            return kw in tokens
    else:

        def _has(kw: str) -> bool:
            return kw in title_lower

    exec_keywords = {"ceo", "coo", "president", "owner", "founder", "vp", "svp", "evp"}
    ops_keywords = {"operations", "ops", "plant", "facility", "production", "supply"}
    proc_keywords = {"procurement", "purchasing", "buyer", "sourcing"}
    tech_keywords = {"engineer", "technical", "r&d", "research", "quality"}
    for kw in exec_keywords:
        if _has(kw):
            return InferenceResult(
                "buyer_persona", "executive", 0.85, "infer_buyer_persona", rationale=f"title={title}"
            )
    for kw in proc_keywords:
        if _has(kw):
            return InferenceResult(
                "buyer_persona", "procurement", 0.83, "infer_buyer_persona", rationale=f"title={title}"
            )
    for kw in ops_keywords:
        if _has(kw):
            return InferenceResult(
                "buyer_persona", "operations", 0.80, "infer_buyer_persona", rationale=f"title={title}"
            )
    for kw in tech_keywords:
        if _has(kw):
            return InferenceResult(
                "buyer_persona", "technical", 0.78, "infer_buyer_persona", rationale=f"title={title}"
            )
    return InferenceResult(
        "buyer_persona", "unknown", 0.55, "infer_buyer_persona", rationale=f"title={title} — no match"
    )


def list_registered_rules() -> list[str]:
    return sorted(_RULE_REGISTRY.keys())
