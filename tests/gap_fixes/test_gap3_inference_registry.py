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


# -- RU-084 / RG-113: USPS first-digit zones + country guard ------------------


@pytest.mark.parametrize(
    ("postal_code", "region"),
    [
        ("02108", "Northeast"),  # Boston MA (zone 0)
        ("10115", "Northeast"),  # New York NY (zone 1)
        ("30301", "Southeast"),  # Atlanta GA (zone 3)
        ("33101", "Southeast"),  # Miami FL (zone 3)
        ("37201", "Southeast"),  # Nashville TN (zone 3)
        ("55401", "Midwest"),  # Minneapolis MN (zone 5)
        ("60601", "Midwest"),  # Chicago IL (zone 6)
        ("64101", "Midwest"),  # Kansas City MO (zone 6)
        ("75201", "South"),  # Dallas TX (zone 7)
        ("94105", "West"),  # San Francisco CA (zone 9)
        ("80202-1234", "West"),  # Denver CO (zone 8), ZIP+4
    ],
)
def test_geography_follows_usps_first_digit_zones(postal_code: str, region: str) -> None:
    result = execute_rule("infer_geography_from_postal_code", {"postal_code": postal_code}, CTX)
    assert result is not None
    assert result.value == region
    assert result.field_name == "region"
    assert result.confidence == pytest.approx(0.82)


@pytest.mark.parametrize(
    "entity",
    [
        {"postal_code": "75001", "country": "FR"},  # Paris
        {"postal_code": "10115", "country": "DE"},  # Berlin
        {"zip_code": "30301", "country": "Canada"},
    ],
)
def test_geography_declines_non_us_country(entity: dict[str, str]) -> None:
    assert execute_rule("infer_geography_from_postal_code", entity, CTX) is None


@pytest.mark.parametrize("country", ["US", "USA", "United States", "us", " usa "])
def test_geography_accepts_us_designations(country: str) -> None:
    result = execute_rule("infer_geography_from_postal_code", {"postal_code": "30301", "country": country}, CTX)
    assert result is not None
    assert result.value == "Southeast"


def test_geography_absent_country_still_emits() -> None:
    """R4-DEC-07 (recommended): no declared country keeps the US inference."""
    result = execute_rule("infer_geography_from_postal_code", {"postal_code": "94105"}, CTX)
    assert result is not None
    assert result.value == "West"


@pytest.mark.parametrize("country", ["", " ", "\t"])
def test_geography_blank_country_counts_as_absent(country: str) -> None:
    """A blank optional ``country`` is missing data, not a declared non-US country."""
    result = execute_rule("infer_geography_from_postal_code", {"postal_code": "94105", "country": country}, CTX)
    assert result is not None
    assert result.value == "West"


@pytest.mark.parametrize(
    ("entity", "region"),
    [
        ({"postal_code": "30301"}, "Midwest"),
        ({"postal_code": "60601"}, "South"),
        ({"postal_code": "75001", "country": "FR"}, "South"),
    ],
)
def test_geography_flag_off_restores_threshold_ladder(
    monkeypatch: pytest.MonkeyPatch, entity: dict[str, str], region: str
) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "inference_geo_usps_zones_enabled", False)
    result = execute_rule("infer_geography_from_postal_code", entity, CTX)
    assert result is not None
    assert result.value == region


# -- RU-088 / RG-117: persona keywords match whole title tokens ---------------
#
# Stage 2 follow-up: the personas, keywords, precedence and confidences are the
# domain's declaration (plugins.inference_rule_parameters.infer_buyer_persona),
# passed to the rule in InferenceContext.rule_parameters. This is a test
# domain's declaration; the engine carries none.

PERSONA_PARAMETERS = {
    "personas": [
        {
            "persona": "executive",
            "keywords": ["ceo", "coo", "president", "owner", "founder", "vp", "svp", "evp"],
            "confidence": 0.85,
        },
        {"persona": "procurement", "keywords": ["procurement", "purchasing", "buyer", "sourcing"], "confidence": 0.83},
        {"persona": "operations", "keywords": ["operations", "ops", "production", "supply"], "confidence": 0.80},
        {
            "persona": "technical",
            "keywords": ["engineer", "technical", "r&d", "research", "quality"],
            "confidence": 0.78,
        },
    ],
    "unmatched": {"persona": "unknown", "confidence": 0.55},
}
PERSONA_CTX = InferenceContext(
    tenant_id="t1",
    domain_id="orbital-logistics",
    pass_number=1,
    rule_parameters={"infer_buyer_persona": PERSONA_PARAMETERS},
)


@pytest.mark.parametrize(
    ("title", "persona"),
    [
        ("Marketing Coordinator", "unknown"),  # 'coo' inside 'coordinator'
        ("Cooperative Liaison", "unknown"),  # 'coo' inside 'cooperative'
        ("Avp-less Analyst", "unknown"),  # 'vp' inside 'avp'
        ("DevOps Engineer", "technical"),  # 'ops' inside 'devops'; 'engineer' matches
        ("Chief Operating Officer COO", "executive"),
        ("VP Sales", "executive"),
        ("SVP, Global Sourcing", "executive"),  # precedence: executive before procurement
        ("Product Owner", "executive"),
        ("Head of Purchasing", "procurement"),
        ("Plant Operations Lead", "operations"),
        ("R&D Manager", "technical"),  # '&' kept inside the token
        ("Quality/Research Analyst", "technical"),
    ],
)
def test_buyer_persona_matches_whole_tokens(title: str, persona: str) -> None:
    result = execute_rule("infer_buyer_persona", {"job_title": title}, PERSONA_CTX)
    assert result is not None
    assert result.value == persona


def test_buyer_persona_confidences_unchanged() -> None:
    expected = {"VP Sales": 0.85, "Buyer": 0.83, "Ops Manager": 0.80, "Engineer": 0.78, "Analyst": 0.55}
    for title, confidence in expected.items():
        result = execute_rule("infer_buyer_persona", {"job_title": title}, PERSONA_CTX)
        assert result is not None
        assert result.confidence == pytest.approx(confidence), title


@pytest.mark.parametrize(
    ("title", "persona"),
    [("Marketing Coordinator", "executive"), ("DevOps Engineer", "operations")],
)
def test_buyer_persona_flag_off_restores_substring_match(
    monkeypatch: pytest.MonkeyPatch, title: str, persona: str
) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "inference_persona_token_match_enabled", False)
    result = execute_rule("infer_buyer_persona", {"job_title": title}, PERSONA_CTX)
    assert result is not None
    assert result.value == persona


# -- RU-089 / RG-118: per-field parsing in company-size and email-domain rules --


@pytest.mark.parametrize(
    ("entity", "tier"),
    [
        ({"employee_count": "1,200", "annual_revenue_usd": 50e6}, "mid_market"),  # revenue fallback
        ({"employee_count": 0, "employees": 5000}, "micro"),  # 0 is a value, not absent
        ({"annual_revenue_usd": 0, "revenue": 5e8}, "micro"),
        ({"employees": 300}, "enterprise"),
        ({"employee_count": "abc", "revenue": 2e6}, "small"),
    ],
)
def test_company_size_tier_parses_each_field(entity: dict[str, object], tier: str) -> None:
    result = execute_rule("infer_company_size_tier", entity, CTX)
    assert result is not None
    assert result.value == tier


def test_company_size_tier_unparseable_everything_is_none() -> None:
    assert execute_rule("infer_company_size_tier", {"employee_count": "n/a", "annual_revenue_usd": "?"}, CTX) is None


@pytest.mark.parametrize(
    ("website", "domain"),
    [
        ("https://acme.com:8443/x", "acme.com"),
        ("https://user:pw@www.acme.com/login", "acme.com"),
        ("acme.com/about", "acme.com"),
        ("www.Example.ORG?q=1", "example.org"),
        ("http://www.example.org", "example.org"),
    ],
)
def test_email_domain_uses_url_hostname(website: str, domain: str) -> None:
    result = execute_rule("infer_email_domain_from_website", {"website": website}, CTX)
    assert result is not None
    assert result.value == domain


@pytest.mark.parametrize("website", ["localhost", "https://", "http://[::1"])
def test_email_domain_rejects_hostless_or_dotless(website: str) -> None:
    assert execute_rule("infer_email_domain_from_website", {"website": website}, CTX) is None


@pytest.mark.parametrize(
    ("rule", "entity", "expected"),
    [
        ("infer_company_size_tier", {"employee_count": "1,200", "annual_revenue_usd": 50e6}, None),
        ("infer_company_size_tier", {"employee_count": 0, "employees": 5000}, "large_enterprise"),
        ("infer_email_domain_from_website", {"website": "https://acme.com:8443/x"}, "acme.com:8443"),
    ],
)
def test_input_parsing_flag_off_restores_legacy(
    monkeypatch: pytest.MonkeyPatch, rule: str, entity: dict[str, object], expected: str | None
) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "inference_input_parsing_strict_enabled", False)
    result = execute_rule(rule, entity, CTX)
    assert (result.value if result is not None else None) == expected


# -- Stage 2 follow-up: persona vocabulary is the domain's declaration --------


def test_buyer_persona_without_a_declaration_infers_nothing() -> None:
    assert execute_rule("infer_buyer_persona", {"job_title": "VP Sales"}, CTX) is None


def test_buyer_persona_uses_only_the_declared_personas() -> None:
    declared = {"personas": [{"persona": "docking_officer", "keywords": ["dockmaster"], "confidence": 0.9}]}
    ctx = InferenceContext(
        tenant_id="t1", domain_id="orbital-logistics", pass_number=1, rule_parameters={"infer_buyer_persona": declared}
    )
    hit = execute_rule("infer_buyer_persona", {"job_title": "Senior Dockmaster"}, ctx)
    assert hit is not None
    assert (hit.value, hit.confidence) == ("docking_officer", 0.9)
    # No built-in keyword survives: 'plant' / 'facility' / 'ceo' mean nothing undeclared.
    for title in ("Plant Manager", "Facility Lead", "CEO"):
        assert execute_rule("infer_buyer_persona", {"job_title": title}, ctx) is None


def test_engine_carries_no_persona_keywords() -> None:
    import inspect

    import engine.inference_rule_registry as registry

    source = inspect.getsource(registry.infer_buyer_persona).lower()
    for former in ('"plant"', '"facility"', '"procurement"', '"ceo"'):
        assert former not in source
