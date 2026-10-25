"""
Tests for GAP-3: inference_rule_registry.py

Domain neutrality (Stage 2, R02): the registry carries only domain-neutral
rules. Business-vertical inference (the former plastics rules) is not
synthesized by any generic fallback, and a domain authorizes which registered
rules may run for it through ``plugins.inference_rules``.
"""

from __future__ import annotations

import pytest

from engine.config.schema import PluginsSpec
from engine.inference_rule_registry import (
    InferenceContext,
    authorized_rules,
    execute_rule,
    list_registered_rules,
)

CTX = InferenceContext(tenant_id="t1", domain_id="orbital-logistics", pass_number=1)

_REMOVED_VERTICAL_RULES = (
    "infer_facility_tier_from_capacity",
    "infer_material_grade_from_mfi",
    "infer_contamination_tolerance",
)


def test_generic_rules_registered() -> None:
    rules = list_registered_rules()
    assert "infer_company_size_tier" in rules
    assert "infer_email_domain_from_website" in rules
    assert "infer_geography_from_postal_code" in rules
    assert "infer_icp_fit_score" in rules
    assert "infer_buyer_persona" in rules


@pytest.mark.parametrize("rule_name", _REMOVED_VERTICAL_RULES)
def test_business_vertical_rules_are_not_built_in(rule_name: str) -> None:
    """Plastics semantics are PlasticOS-owned; the generic engine must not carry them."""
    assert rule_name not in list_registered_rules()
    with pytest.raises(KeyError, match="not found in registry"):
        execute_rule(rule_name, {"processing_capacity_tons_per_year": 50000, "melt_flow_index": 5.0}, CTX)


def test_company_size_tier_from_employee_count() -> None:
    result = execute_rule("infer_company_size_tier", {"employee_count": 120}, CTX)
    assert result is not None
    assert result.value == "mid_market"
    assert result.confidence >= 0.55


def test_email_domain_from_website() -> None:
    result = execute_rule("infer_email_domain_from_website", {"website": "https://www.example.org/about"}, CTX)
    assert result is not None
    assert result.value == "example.org"


def test_icp_fit_score_uses_only_generic_signals() -> None:
    """A vertical-derived field must not move the generic ICP score."""
    base = execute_rule("infer_icp_fit_score", {"company_size_tier": "enterprise", "region": "West"}, CTX)
    with_vertical = execute_rule(
        "infer_icp_fit_score",
        {"company_size_tier": "enterprise", "region": "West", "facility_tier": "large"},
        CTX,
    )
    assert base is not None
    assert with_vertical is not None
    assert with_vertical.value == base.value


def test_unknown_rule_raises() -> None:
    with pytest.raises(KeyError, match="not found in registry"):
        execute_rule("nonexistent_rule", {}, CTX)


def test_low_confidence_suppressed() -> None:
    # icp_fit_score with no contributing factors returns None
    result = execute_rule("infer_icp_fit_score", {}, CTX)
    assert result is None


class _Spec:
    def __init__(self, plugins: PluginsSpec | None) -> None:
        self.plugins = plugins


def test_authorized_rules_default_to_none() -> None:
    """A domain that declares no inference rules authorizes none — never 'all registered'."""
    assert authorized_rules(_Spec(None)) == []
    assert authorized_rules(_Spec(PluginsSpec())) == []


def test_authorized_rules_come_from_domain_plugins() -> None:
    spec = _Spec(PluginsSpec(inference_rules=["infer_company_size_tier", "infer_email_domain_from_website"]))
    assert authorized_rules(spec) == ["infer_company_size_tier", "infer_email_domain_from_website"]
