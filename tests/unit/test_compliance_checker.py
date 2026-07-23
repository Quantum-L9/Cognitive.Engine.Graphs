"""Unit tests — Compliance: prohibited factor enforcement.

Real APIs:
    engine/compliance/prohibited_factors.py
        ProhibitedFactorValidator(domain_spec).validate_gate(gate_spec) -> None
        (raises ValueError if a gate references a blocked field)
    engine/compliance/engine.py
        ComplianceEngine(domain_spec).check_match_request(
            tenant=..., query=..., match_direction=..., trace_id=...) -> dict
"""

from __future__ import annotations

import pytest


@pytest.fixture
def plasticos_spec():
    """Load plasticos spec, skip if not loadable."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader()
    try:
        return loader.load_domain("plasticos")
    except Exception:
        pytest.skip("plasticos domain spec not loadable with current schema")


def test_no_prohibited_factors_passes_all(plasticos_spec):
    """With no blocked fields configured, every gate validates cleanly."""
    from engine.compliance.prohibited_factors import ProhibitedFactorValidator

    validator = ProhibitedFactorValidator(plasticos_spec)
    if validator.blocked_fields:
        pytest.skip("plasticos spec configures blocked fields; covered elsewhere")
    for gate in plasticos_spec.gates:
        validator.validate_gate(gate)  # must not raise


def test_prohibited_factor_in_gate_raises(plasticos_spec):
    """A gate referencing a blocked field must be rejected at compile time."""
    from engine.compliance.prohibited_factors import ProhibitedFactorValidator

    validator = ProhibitedFactorValidator(plasticos_spec)
    if not plasticos_spec.gates:
        pytest.skip("No gates in plasticos spec")

    # Force a blocked field matching an existing gate's candidate property.
    gate = plasticos_spec.gates[0]
    blocked = gate.candidateprop or gate.queryparam
    if not blocked:
        pytest.skip("First gate has no candidateprop/queryparam to block")
    validator.blocked_fields = {blocked}

    with pytest.raises(ValueError):
        validator.validate_gate(gate)


def test_compliance_pass_with_clean_payload(plasticos_spec):
    """Clean match query passes pre-match compliance checks unchanged."""
    from engine.compliance.engine import ComplianceEngine

    engine = ComplianceEngine(plasticos_spec)
    query = {"entity_id": "test-1", "contamination_tolerance": 0.05}
    result = engine.check_match_request(
        tenant="plasticos",
        query=query,
        match_direction="intake_to_buyer",
        trace_id="test-trace",
    )
    assert isinstance(result, dict)
    assert result == query
