import pytest

from engine.arbitration.engine import ArbitrationEngine
from engine.arbitration.schema import ArbitrationInput
from engine.config.schema import DecisionPolicy, HardConstraint, PolicyThresholds, PolicyWeights

pytestmark = pytest.mark.unit


def _make_test_policy() -> DecisionPolicy:
    """Create a test policy matching tests/fixtures/design_packs/plasticos/spec.yaml decision_policy section."""
    return DecisionPolicy(
        version="1.0.0",
        hard_constraints=[
            HardConstraint(metric="compliance_pass", operator="eq", value=True),
            HardConstraint(metric="risk", operator="lte", value=0.70),
        ],
        weights=PolicyWeights(revenue=0.40, margin=0.30, risk=0.20, capacity=0.10),
        thresholds=PolicyThresholds(
            approve_threshold=0.50,
            reject_threshold=0.20,
            conflict_tolerance=0.35,
        ),
    )


def test_arbitration_rejects_on_hard_constraint() -> None:
    """Risk > 0.70 violates hard constraint → reject."""
    policy = _make_test_policy()
    engine = ArbitrationEngine()
    result = engine.resolve(
        policy,
        ArbitrationInput(revenue=0.9, margin=0.8, risk=0.9, capacity=0.7, compliance_pass=True),
    )
    assert result.final_decision == "reject"


def test_arbitration_approves_on_high_composite() -> None:
    """High revenue/margin, low risk, high capacity → approve."""
    policy = _make_test_policy()
    engine = ArbitrationEngine()
    result = engine.resolve(
        policy,
        ArbitrationInput(revenue=0.9, margin=0.8, risk=0.2, capacity=0.9, compliance_pass=True),
    )
    assert result.final_decision == "approve"
    assert result.composite_score >= 0.5


def test_arbitration_input_rejects_out_of_range() -> None:
    """ArbitrationInput rejects values outside [0, 1]."""
    with pytest.raises(ValueError):
        ArbitrationInput(revenue=1.2, margin=0.0, risk=0.0, capacity=0.0, compliance_pass=True)


# RU-107 / CEG-COR-130: NaN fails the inclusion test for every metric.
@pytest.mark.parametrize("metric", ["revenue", "margin", "risk", "capacity"])
def test_arbitration_input_rejects_nan(metric: str) -> None:
    from pydantic import ValidationError

    values = {"revenue": 0.5, "margin": 0.5, "risk": 0.5, "capacity": 0.5, metric: float("nan")}
    with pytest.raises(ValidationError, match=metric):
        ArbitrationInput(**values, compliance_pass=True)


@pytest.mark.parametrize("bound", [0.0, 1.0])
def test_arbitration_input_accepts_boundaries(bound: float) -> None:
    payload = ArbitrationInput(revenue=bound, margin=bound, risk=bound, capacity=bound, compliance_pass=True)
    assert payload.revenue == bound
