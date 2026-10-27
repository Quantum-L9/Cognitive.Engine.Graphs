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
    # RU-062: `compliance=None` leaves compliance ENABLED (see
    # test_enabled_by_default_when_no_compliance); this passed only because
    # list elements were never traversed. Disable compliance explicitly.
    s = _MockDomainSpec(compliance=_MockComplianceDisabled(enabled=False))
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


# ── RU-062 / CEG-COR-009: enabled redact_response reaches match candidates ──


def _healthcare_engine() -> ComplianceEngine:
    import yaml

    from engine.config.schema import DomainSpec

    raw = yaml.safe_load(Path("domains/healthcare-referral/spec.yaml").read_text(encoding="utf-8"))
    return ComplianceEngine(DomainSpec.model_validate(raw))


def test_redact_response_removes_pii_inside_match_candidates() -> None:
    import copy

    response = {
        "candidates": [{"candidate": {"contact_email": "a@b.com", "ssn": "123-45-6789", "npi": "N1"}, "score": 0.9}],
        "query_id": "q_5c9382647927",
    }
    before = copy.deepcopy(response)
    out = _healthcare_engine().redact_response(response, "t")
    assert response == before  # caller's object untouched
    candidate = out["candidates"][0]["candidate"]
    assert "a@b.com" not in str(candidate)
    assert "123-45-6789" not in str(candidate)
    assert candidate["npi"] == "N1"
    assert out["candidates"][0]["score"] == 0.9
    assert out["query_id"] == "q_5c9382647927"


def test_check_sync_request_audits_nested_pii_fields() -> None:
    engine = _healthcare_engine()
    engine.check_sync_request(
        tenant="t", entity_type="Patient", batch=[{"entity_id": "p1", "contacts": [{"email": "x@y.com"}]}]
    )
    entries = engine._audit.flush_sync()
    access = [e for e in entries if e.action == "access"]
    assert len(access) == 1
    assert access[0].pii_fields_accessed == ["contacts[0].email"]


def test_redact_response_flag_off_keeps_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "pii_redaction_recursive", False)
    response = {"candidates": [{"candidate": {"contact_email": "a@b.com"}, "score": 0.9}]}
    assert _healthcare_engine().redact_response(response, "t") == response


# ── RU-063 / CEG-COR-050: every violation is audited before it propagates ──


def _mortgage_spec_audit(audit_on_violation: bool = True):
    import yaml

    from engine.config.schema import DomainSpec

    raw = yaml.safe_load(Path("domains/mortgage-brokerage/spec.yaml").read_text(encoding="utf-8"))
    raw["compliance"]["prohibitedfactors"]["auditonviolation"] = audit_on_violation
    return DomainSpec.model_validate(raw)


def _denied(engine: ComplianceEngine) -> list:
    return [e for e in engine._audit.flush_sync() if e.outcome == "denied"]


def test_blocked_gate_emits_one_denied_entry_then_raises() -> None:
    from engine.config.schema import GateSpec

    engine = ComplianceEngine(_mortgage_spec_audit())
    engine._audit.flush_sync()
    gate = GateSpec.model_validate({"name": "age_gate", "type": "threshold", "candidateprop": "age"})
    with pytest.raises(ValueError, match="Gate 'age_gate' references prohibited field 'age'"):
        engine.validate_gates([gate], tenant="acme")
    denied = _denied(engine)
    assert len(denied) == 1
    assert "age_gate" in denied[0].resource
    assert denied[0].metadata["blocked_field"] == "age"
    assert denied[0].actor == "acme"


def test_blocked_scoring_dimension_emits_denied_entry() -> None:
    from engine.config.schema import ScoringDimensionSpec

    engine = ComplianceEngine(_mortgage_spec_audit())
    engine._audit.flush_sync()
    dim = ScoringDimensionSpec.model_validate(
        {
            "name": "age_dim",
            "source": "candidateproperty",
            "candidateprop": "Age",
            "computation": "candidateproperty",
            "weightkey": "w",
            "defaultweight": 0.1,
        }
    )
    with pytest.raises(ValueError, match="prohibited field"):
        engine.validate_scoring([dim])
    denied = _denied(engine)
    assert len(denied) == 1
    assert "age_dim" in denied[0].resource
    assert denied[0].metadata["blocked_field"] == "Age"


def test_blocked_sync_row_emits_denied_entry_without_value() -> None:
    engine = ComplianceEngine(_mortgage_spec_audit())
    engine._audit.flush_sync()
    with pytest.raises(ValueError, match="prohibited field 'race'"):
        engine.check_sync_request(
            tenant="acme", entity_type="Borrower", batch=[{"entity_id": "b1", "coborrowers": [{"race": "SECRET"}]}]
        )
    denied = _denied(engine)
    assert len(denied) == 1
    assert denied[0].metadata["blocked_field"] == "race"
    assert "SECRET" not in denied[0].model_dump_json()


def test_no_violation_audit_when_auditonviolation_false() -> None:
    from engine.config.schema import GateSpec

    engine = ComplianceEngine(_mortgage_spec_audit(audit_on_violation=False))
    engine._audit.flush_sync()
    gate = GateSpec.model_validate({"name": "g", "type": "threshold", "candidateprop": "age"})
    with pytest.raises(ValueError):
        engine.validate_gates([gate])
    assert _denied(engine) == []


def test_no_violation_audit_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.schema import GateSpec
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "prohibited_factor_violation_audit", False)
    engine = ComplianceEngine(_mortgage_spec_audit())
    engine._audit.flush_sync()
    with pytest.raises(ValueError):
        engine.validate_gates([GateSpec.model_validate({"name": "g", "type": "threshold", "candidateprop": "age"})])
    assert _denied(engine) == []


# ── RU-064 / CEG-COR-051: declared compliance.pii fields and handling are applied ──


def _spec_from(path: str, mutate=None):
    import yaml

    from engine.config.schema import DomainSpec

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if mutate:
        mutate(raw)
    return DomainSpec.model_validate(raw)


def _match_response(candidate: dict) -> dict:
    return {"candidates": [{"candidate": candidate, "score": 0.8}], "query_id": "q_5c9382647927"}


def test_healthcare_declared_fields_are_hashed_not_deleted() -> None:
    from engine.compliance.pii import PIIHandler

    engine = ComplianceEngine(_spec_from("domains/healthcare-referral/spec.yaml"))
    out = engine.redact_response(_match_response({"name": "Dr Jane Roe", "patientid": "PT-991", "npi": "N1"}), "t")
    candidate = out["candidates"][0]["candidate"]
    assert candidate["name"] == PIIHandler.hash_value("Dr Jane Roe")
    assert candidate["patientid"] == PIIHandler.hash_value("PT-991")
    assert len(candidate["name"]) == 64
    assert candidate["npi"] == "N1"
    assert out["query_id"] == "q_5c9382647927"


def test_declared_field_match_is_case_insensitive_at_any_depth() -> None:
    engine = ComplianceEngine(_spec_from("domains/healthcare-referral/spec.yaml"))
    out = engine.redact_response({"rows": [{"nested": {"PatientId": "PT-1"}}]}, "t")
    assert out["rows"][0]["nested"]["PatientId"] != "PT-1"


def test_legal_discovery_declared_redact_uses_fixed_marker() -> None:
    from engine.compliance.pii import REDACTION_MARKER

    engine = ComplianceEngine(_spec_from("domains/legal-discovery/spec.yaml"))
    out = engine.redact_response({"clientname": "Acme LLP", "ssn": "123-45-6789", "matter": "M-1"}, "t")
    assert out == {"clientname": REDACTION_MARKER, "ssn": REDACTION_MARKER, "matter": "M-1"}


@pytest.mark.parametrize("handling", ["encrypt", "tokenize"])
def test_unimplemented_handling_fails_closed_to_redact(handling: str, caplog: pytest.LogCaptureFixture) -> None:
    from engine.compliance.pii import REDACTION_MARKER

    def mutate(raw: dict) -> None:
        raw["compliance"]["pii"]["handling"] = handling

    engine = ComplianceEngine(_spec_from("domains/legal-discovery/spec.yaml", mutate))
    with caplog.at_level("WARNING"):
        out = engine.redact_response({"clientname": "Acme LLP"}, "t")
    assert out == {"clientname": REDACTION_MARKER}
    assert "Acme LLP" not in caplog.text
    assert handling in caplog.text


def test_pii_enabled_false_turns_declared_handling_off() -> None:
    def mutate(raw: dict) -> None:
        raw["compliance"]["pii"]["enabled"] = False

    engine = ComplianceEngine(_spec_from("domains/healthcare-referral/spec.yaml", mutate))
    out = engine.redact_response(_match_response({"name": "Dr Jane Roe", "patientid": "PT-991"}), "t")
    assert out["candidates"][0]["candidate"] == {"name": "Dr Jane Roe", "patientid": "PT-991"}


def test_declared_fields_flag_off_restores_builtin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "pii_declared_fields_enforced", False)
    engine = ComplianceEngine(_spec_from("domains/healthcare-referral/spec.yaml"))
    out = engine.redact_response({"name": "Dr Jane Roe", "ssn": "123-45-6789"}, "t")
    assert out == {"name": "Dr Jane Roe"}  # built-in inference deletes ssn; name survives
