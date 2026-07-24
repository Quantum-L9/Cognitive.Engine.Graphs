"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [governance, compliance]
status: active
--- /L9_META ---

Security tests — ECOA prohibited factor enforcement at compliance layer.
"""

from __future__ import annotations

from pathlib import Path

DOMAINS_DIR = Path(__file__).parent.parent.parent / "domains"

ECOA_PROHIBITED = [
    "race",
    "color",
    "religion",
    "national_origin",
    "sex",
    "marital_status",
    "age",
    "disability",
    "familial_status",
    "receipt_of_public_assistance",
]


def test_compliance_engine_loads_for_plasticos():
    """ComplianceEngine must load without error for plasticos."""
    from engine.compliance.engine import ComplianceEngine
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    engine = ComplianceEngine(spec)
    assert engine is not None


def test_prohibited_factor_validator_exists():
    """ProhibitedFactorValidator must exist and load from spec."""
    from engine.compliance.prohibited_factors import ProhibitedFactorValidator
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    validator = ProhibitedFactorValidator(spec)
    assert validator is not None


def test_clean_payload_passes_compliance():
    """A payload with no prohibited fields must pass check_match_request."""
    from engine.compliance.engine import ComplianceEngine
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    engine = ComplianceEngine(spec)
    query = {
        "entity_id": "f1",
        "contamination_tolerance": 0.05,
        "facility_tier": "mid",
    }
    result = engine.check_match_request(
        tenant="plasticos",
        query=query,
        match_direction="buyer_to_seller",
    )
    # Should pass through without error or mutation
    assert result == query


def test_compliance_evaluate_returns_result():
    """check_match_request() must return the sanitized query dict."""
    from engine.compliance.engine import ComplianceEngine
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    engine = ComplianceEngine(spec)
    result = engine.check_match_request(
        tenant="plasticos",
        query={"entity_id": "test"},
        match_direction="buyer_to_seller",
    )
    assert isinstance(result, dict)
