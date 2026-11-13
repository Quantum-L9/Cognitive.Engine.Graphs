"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, compliance, prohibited-factors]
owner: engine-team
status: active
--- /L9_META ---

CRITICAL: Compliance tests for prohibited factors (ECOA, HIPAA, FMCSA, etc.)
Target Coverage: 95%+

These tests verify that the engine correctly blocks prohibited factors at
compile-time, preventing discriminatory matching criteria.
"""

from unittest.mock import MagicMock

import pytest

from engine.compliance.prohibited_factors import ProhibitedFactorValidator
from engine.config.schema import DomainSpec, GateSpec
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT


def make_mock_domain_spec(blocked_fields: list[str] | None = None, enabled: bool = True) -> MagicMock:
    """Create a mock DomainSpec with prohibited factors config."""
    spec = MagicMock(spec=DomainSpec)
    spec.compliance = MagicMock()
    spec.compliance.prohibitedfactors = MagicMock()
    spec.compliance.prohibitedfactors.enabled = enabled
    spec.compliance.prohibitedfactors.blockedfields = blocked_fields or []
    return spec


def make_gate_spec(name: str, candidate_prop: str, query_param: str = "value") -> MagicMock:
    """Create a mock GateSpec."""
    gate = MagicMock(spec=GateSpec)
    gate.name = name
    gate.candidateprop = candidate_prop
    gate.queryparam = query_param
    return gate


# ============================================================================
# ECOA COMPLIANCE TESTS (Equal Credit Opportunity Act)
# ============================================================================


@pytest.mark.compliance
@pytest.mark.unit
class TestECOACompliance:
    """Test ECOA prohibited factor enforcement."""

    def test_ecoa_blocks_race_in_gate(self) -> None:
        """ECOA: Race in gate candidateprop should raise error."""
        spec = make_mock_domain_spec(blocked_fields=["race", "ethnicity", "gender"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("race_gate", candidate_prop="race")

        with pytest.raises(ValueError) as exc_info:
            validator.validate_gate(gate)

        assert "race" in str(exc_info.value).lower()

    def test_ecoa_blocks_ethnicity_in_gate(self) -> None:
        """ECOA: Ethnicity in gate should raise error."""
        spec = make_mock_domain_spec(blocked_fields=["race", "ethnicity", "gender"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("ethnicity_gate", candidate_prop="ethnicity")

        with pytest.raises(ValueError):
            validator.validate_gate(gate)

    def test_ecoa_blocks_gender_in_query_param(self) -> None:
        """ECOA: Gender in queryparam should raise error."""
        spec = make_mock_domain_spec(blocked_fields=["race", "ethnicity", "gender"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("gender_gate", candidate_prop="acceptsmale", query_param="gender")

        with pytest.raises(ValueError):
            validator.validate_gate(gate)

    def test_ecoa_allows_creditscore(self) -> None:
        """ECOA: Credit score (non-prohibited) should be allowed."""
        spec = make_mock_domain_spec(blocked_fields=["race", "ethnicity", "gender"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("credit_min", candidate_prop="mincreditscore", query_param="creditscore")

        # Should not raise
        validator.validate_gate(gate)

    def test_ecoa_allows_income(self) -> None:
        """ECOA: Income (non-prohibited) should be allowed."""
        spec = make_mock_domain_spec(blocked_fields=["race", "ethnicity", "gender"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("income_gate", candidate_prop="annualincome")

        # Should not raise
        validator.validate_gate(gate)


# ============================================================================
# HIPAA COMPLIANCE TESTS
# ============================================================================


@pytest.mark.compliance
@pytest.mark.unit
class TestHIPAACompliance:
    """Test HIPAA prohibited factor enforcement."""

    def test_hipaa_blocks_genetic_information(self) -> None:
        """HIPAA: Genetic information in gate should raise error."""
        spec = make_mock_domain_spec(blocked_fields=["geneticinformation", "disability"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("genetic_gate", candidate_prop="geneticinformation")

        with pytest.raises(ValueError):
            validator.validate_gate(gate)

    def test_hipaa_allows_medical_specialty(self) -> None:
        """HIPAA: Medical specialty (non-prohibited) should be allowed."""
        spec = make_mock_domain_spec(blocked_fields=["geneticinformation", "disability"])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("specialty_gate", candidate_prop="specialty")

        # Should not raise
        validator.validate_gate(gate)


# ============================================================================
# DISABLED / EMPTY ENFORCEMENT TESTS
# ============================================================================


@pytest.mark.compliance
@pytest.mark.unit
class TestEnforcementModes:
    """Test different enforcement configurations."""

    def test_disabled_enforcement_allows_all(self) -> None:
        """Disabled enforcement should allow all fields."""
        spec = make_mock_domain_spec(blocked_fields=["race"], enabled=False)
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("race_gate", candidate_prop="race")

        # Should not raise when disabled
        validator.validate_gate(gate)

    def test_empty_blocked_fields_allows_all(self) -> None:
        """Empty blocked fields list should allow all fields."""
        spec = make_mock_domain_spec(blocked_fields=[])
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("any_gate", candidate_prop="race")

        # Should not raise
        validator.validate_gate(gate)

    def test_none_compliance_config_allows_all(self) -> None:
        """None compliance config should allow all fields."""
        spec = MagicMock(spec=DomainSpec)
        spec.compliance = None
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("any_gate", candidate_prop="race")

        # Should not raise
        validator.validate_gate(gate)

    def test_none_prohibited_factors_allows_all(self) -> None:
        """None prohibitedfactors config should allow all fields."""
        spec = MagicMock(spec=DomainSpec)
        spec.compliance = MagicMock()
        spec.compliance.prohibitedfactors = None
        validator = ProhibitedFactorValidator(spec)

        gate = make_gate_spec("any_gate", candidate_prop="race")

        # Should not raise
        validator.validate_gate(gate)


# ============================================================================
# RU-061 / CEG-COR-008: every compiled property reference, casefolded
# ============================================================================

_MORTGAGE_SPEC_PATH = DESIGN_PACKS_ROOT / "mortgage-brokerage/spec.yaml"


def _mortgage_spec() -> DomainSpec:
    import yaml

    return DomainSpec.model_validate(yaml.safe_load(_MORTGAGE_SPEC_PATH.read_text(encoding="utf-8")))


def _gate(**fields: object) -> GateSpec:
    return GateSpec.model_validate({"name": "g", **fields})


def _dim(**fields: object):
    from engine.config.schema import ScoringDimensionSpec

    base = {
        "name": "d",
        "source": "candidateproperty",
        "computation": "candidateproperty",
        "weightkey": "w_d",
        "defaultweight": 0.1,
    }
    return ScoringDimensionSpec.model_validate({**base, **fields})


@pytest.mark.compliance
@pytest.mark.unit
class TestFullPropertyCoverage:
    """Mortgage-brokerage blocks `age`/`race`/`gender`: no compiled field may reference them."""

    @pytest.mark.parametrize(
        "gate_fields",
        [
            {"type": "selfrange", "candidateprop_min": "age", "candidateprop_max": "maxloan"},
            {"type": "range", "candidateprop": "loanamount", "queryparam_max": "age"},
            {"type": "range", "candidateprop": "loanamount", "queryparam_min": "AGE"},
            {"type": "temporalrange", "candidateprop_start": "age", "candidateprop_end": "x_end"},
            {"type": "temporalrange", "queryparam_start": "Age", "queryparam_end": "x_end"},
            {"type": "temporalrange", "queryparam_start": "x_start", "queryparam_end": "age"},
            {"type": "selfrange", "candidateprop_min": "minx", "candidateprop_max": "age"},
            {"type": "temporalrange", "candidateprop_start": "s", "candidateprop_end": "Age"},
            {"type": "threshold", "candidateprop": "Race", "queryparam": "x"},
            {"type": "threshold", "candidateprop": "x", "queryparam": "GENDER"},
        ],
    )
    def test_compliance_engine_blocks_every_property_reference(self, gate_fields: dict) -> None:
        from engine.compliance.engine import ComplianceEngine

        engine = ComplianceEngine(_mortgage_spec())
        with pytest.raises(ValueError, match="prohibited field") as exc_info:
            engine.validate_gates([_gate(**gate_fields)])
        assert "'g'" in str(exc_info.value)

    def test_literal_queryparam_is_not_a_field_name(self) -> None:
        validator = ProhibitedFactorValidator(_mortgage_spec())
        validator.validate_gate(_gate(type="threshold", candidateprop="creditscore", queryparam=85.0))
        validator.validate_gate(_gate(type="boolean", candidateprop="active", queryparam=True))

    def test_allowed_gates_still_pass(self) -> None:
        validator = ProhibitedFactorValidator(_mortgage_spec())
        validator.validate_gate(_gate(type="threshold", candidateprop="mincreditscore", queryparam="creditscore"))
        validator.validate_gate(_gate(type="threshold", candidateprop="annualincome", queryparam="income"))
        validator.validate_gate(_gate(type="selfrange", candidateprop_min="minloan", candidateprop_max="maxloan"))

    @pytest.mark.parametrize(
        ("dim_fields", "named"),
        [({"candidateprop": "age"}, "age"), ({"source": "queryproperty", "queryprop": "Gender"}, "Gender")],
    )
    def test_scoring_dimension_on_blocked_field_raises(self, dim_fields: dict, named: str) -> None:
        from engine.compliance.engine import ComplianceEngine

        engine = ComplianceEngine(_mortgage_spec())
        with pytest.raises(ValueError, match="prohibited field") as exc_info:
            engine.validate_scoring([_dim(**dim_fields)])
        assert "'d'" in str(exc_info.value)
        assert named in str(exc_info.value)

    def test_shipped_mortgage_dimensions_pass(self) -> None:
        from engine.compliance.engine import ComplianceEngine

        spec = _mortgage_spec()
        engine = ComplianceEngine(spec)
        engine.validate_gates(spec.gates)
        engine.validate_scoring(spec.scoring.dimensions)

    def test_plasticos_with_empty_blockedfields_passes(self) -> None:
        import yaml

        from engine.compliance.engine import ComplianceEngine

        spec = DomainSpec.model_validate(
            yaml.safe_load((DESIGN_PACKS_ROOT / "plasticos/spec.yaml").read_text(encoding="utf-8"))
        )
        engine = ComplianceEngine(spec)
        engine.validate_gates(spec.gates)
        engine.validate_scoring(spec.scoring.dimensions)

    def test_disabled_prohibitedfactors_leaves_every_check_dormant(self) -> None:
        spec = _mortgage_spec()
        pf = spec.compliance.prohibitedfactors.model_copy(update={"enabled": False})
        spec = spec.model_copy(update={"compliance": spec.compliance.model_copy(update={"prohibitedfactors": pf})})
        validator = ProhibitedFactorValidator(spec)
        validator.validate_gate(_gate(type="threshold", candidateprop="race", queryparam="age"))
        validator.validate_scoring_dimension(_dim(candidateprop="age"))
        validator.validate_sync_row({"race": "x"})

    def test_flag_off_restores_two_field_exact_case_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "prohibited_factor_full_coverage", False)
        validator = ProhibitedFactorValidator(_mortgage_spec())
        # legacy: case variants and min/max/start/end fields pass, dimensions and rows are unchecked
        validator.validate_gate(_gate(type="threshold", candidateprop="Race", queryparam="x"))
        validator.validate_gate(_gate(type="selfrange", candidateprop_min="age", candidateprop_max="y"))
        validator.validate_scoring_dimension(_dim(candidateprop="age"))
        validator.validate_sync_row({"Race": "x", "kids": [{"race": "y"}]})
        # legacy: exact-case candidateprop/queryparam still block
        with pytest.raises(ValueError, match="prohibited field 'race'"):
            validator.validate_gate(_gate(type="threshold", candidateprop="race", queryparam="x"))
