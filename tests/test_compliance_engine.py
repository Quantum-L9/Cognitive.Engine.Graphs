"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, compliance]
owner: engine-team
status: active
--- /L9_META ---

Tests for ComplianceEngine orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from engine.compliance.engine import ComplianceEngine


@dataclass
class _MockProhibitedFactors:
    enabled: bool = True
    blockedfields: list[str] = field(default_factory=lambda: ["race", "religion"])


@dataclass
class _MockPII:
    enabled: bool = True


@dataclass
class _MockAudit:
    enabled: bool = True


@dataclass
class _MockCompliance:
    prohibitedfactors: _MockProhibitedFactors = field(default_factory=_MockProhibitedFactors)
    pii: _MockPII = field(default_factory=_MockPII)
    audit: _MockAudit = field(default_factory=_MockAudit)


@dataclass
class _MockDomainSpec:
    compliance: _MockCompliance | None = field(default_factory=_MockCompliance)


def _spec(*, enabled: bool = True, blocked: list[str] | None = None) -> _MockDomainSpec:
    """Typed compliance/domain spec fixture for ComplianceEngine tests."""
    return _MockDomainSpec(
        compliance=_MockCompliance(
            prohibitedfactors=_MockProhibitedFactors(
                enabled=enabled,
                blockedfields=blocked or ["race", "religion"],
            ),
            pii=_MockPII(enabled=enabled),
            audit=_MockAudit(enabled=enabled),
        )
    )


def test_enabled_when_configured() -> None:
    assert ComplianceEngine(_spec()).enabled is True


def test_enabled_by_default_when_no_compliance() -> None:
    """Compliance defaults to enabled for SOC2/HIPAA safety even without explicit config."""
    s = _MockDomainSpec(compliance=None)
    assert ComplianceEngine(s).enabled is True


@dataclass
class _MockComplianceDisabled:
    """Compliance config with enabled=False."""

    enabled: bool = False
    prohibitedfactors: _MockProhibitedFactors = field(default_factory=_MockProhibitedFactors)
    pii: _MockPII = field(default_factory=_MockPII)
    audit: _MockAudit = field(default_factory=_MockAudit)


def test_disabled_when_explicitly_false() -> None:
    """Compliance only disabled when explicitly set to False."""
    s = _MockDomainSpec(compliance=_MockComplianceDisabled(enabled=False))
    assert ComplianceEngine(s).enabled is False


@dataclass
class _MockGate:
    """Typed gate fixture for ComplianceEngine tests."""

    candidateprop: str
    queryparam: str
    name: str


def test_validate_gates_clean() -> None:
    gate = _MockGate(candidateprop="grade", queryparam="min_grade", name="g")
    ComplianceEngine(_spec()).validate_gates([gate])


def test_validate_gates_blocked() -> None:
    gate = _MockGate(candidateprop="race", queryparam="x", name="bad")
    engine = ComplianceEngine(_spec())
    with pytest.raises(ValueError, match="prohibited"):
        engine.validate_gates([gate])


def test_redact_passthrough_when_disabled() -> None:
    s = _MockDomainSpec(compliance=None)
    data = {"candidates": [{"ssn": "123"}]}
    assert ComplianceEngine(s).redact_response(data, "t") == data


# ── RU-061 / CEG-COR-008: sync rows checked casefolded at every depth ──


def _mortgage_engine() -> ComplianceEngine:
    import yaml

    from engine.config.schema import DomainSpec

    return ComplianceEngine(
        DomainSpec.model_validate(
            yaml.safe_load(Path("domains/mortgage-brokerage/spec.yaml").read_text(encoding="utf-8"))
        )
    )


@pytest.mark.parametrize(
    "row",
    [
        {"entity_id": "b1", "Race": "x"},
        {"entity_id": "b1", "coborrowers": [{"id": "c1", "race": "x"}]},
        {"entity_id": "b1", "household": {"members": [{"id": "m1", "profile": {"GENDER": "x"}}]}},
    ],
)
def test_check_sync_request_rejects_blocked_key_at_any_depth(row: dict) -> None:
    with pytest.raises(ValueError, match="prohibited field"):
        _mortgage_engine().check_sync_request(tenant="t", entity_type="Borrower", batch=[row])


def test_check_sync_request_error_names_field_not_value() -> None:
    with pytest.raises(ValueError) as exc_info:
        _mortgage_engine().check_sync_request(
            tenant="t", entity_type="Borrower", batch=[{"entity_id": "b1", "Race": "secret-value"}]
        )
    assert "Race" in str(exc_info.value)
    assert "secret-value" not in str(exc_info.value)


def test_check_sync_request_allows_clean_nested_rows() -> None:
    _mortgage_engine().check_sync_request(
        tenant="t",
        entity_type="Borrower",
        batch=[{"entity_id": "b1", "creditscore": 700, "coborrowers": [{"id": "c1", "income": 1}]}],
    )


def test_check_sync_request_flag_off_is_top_level_exact_case(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "prohibited_factor_full_coverage", False)
    engine = _mortgage_engine()
    engine.check_sync_request(tenant="t", entity_type="Borrower", batch=[{"entity_id": "b1", "Race": "x"}])
    engine.check_sync_request(
        tenant="t", entity_type="Borrower", batch=[{"entity_id": "b1", "coborrowers": [{"race": "x"}]}]
    )
    with pytest.raises(ValueError, match="prohibited field 'race'"):
        engine.check_sync_request(tenant="t", entity_type="Borrower", batch=[{"entity_id": "b1", "race": "x"}])
