"""PACK-026 harvest into PlasticOS DomainSpec (TASK-018)."""

from __future__ import annotations

from pathlib import Path

from engine.config.explanations import (
    assert_no_tensor_runtime,
    order_feature_contributions,
    recommended_actions_for,
)
from engine.config.loader import DomainPackLoader
from engine.gates.compiler import GateCompiler

ROOT = Path(__file__).resolve().parents[2]
DIR2 = "buyer_demand_to_supply_opportunity"
OUTCOMES = {
    "reviewed",
    "contacted",
    "quoted",
    "accepted",
    "rejected",
    "completed",
    "claim_raised",
    "paid",
    "repeat_business",
}


def _spec():
    return DomainPackLoader(config_path=str(ROOT / "domains")).load_domain("plasticos")


def test_no_tensor_runtime_package() -> None:
    assert_no_tensor_runtime()
    assert not (ROOT / "engine" / "tensor").exists()


def test_domain_version_and_feedbackloop_enabled() -> None:
    spec = _spec()
    assert spec.domain.version == "1.1.0"
    assert spec.feedbackloop.enabled is True


def test_harvested_polymer_gate_compiles_for_second_direction() -> None:
    spec = _spec()
    names = {g.name for g in spec.gates}
    assert "active_supply_polymer" in names
    clause = GateCompiler(spec).compile_all_gates(match_direction=DIR2)
    assert isinstance(clause, str)
    assert "polymer_type" in clause


def test_feature_catalog_maps_scoring_dimensions() -> None:
    spec = _spec()
    assert spec.feature_catalog
    dim_names = {d.name for d in spec.scoring.dimensions}
    mapped = [e for e in spec.feature_catalog if e.scoring_dimension]
    assert mapped
    for entry in mapped:
        assert entry.scoring_dimension in dim_names


def test_outcome_signals_match_feedback_contract() -> None:
    spec = _spec()
    signals = {o.signal for o in spec.outcome_signals}
    assert signals == OUTCOMES


def test_explanation_ordering_is_deterministic() -> None:
    spec = _spec()
    contribs = [
        {"feature_id": "recency", "contribution": 0.2},
        {"feature_id": "geo_proximity", "contribution": 0.9},
        {"feature_id": "credit_score", "contribution": 0.1},
    ]
    ordered = order_feature_contributions(contribs, spec.explanations)
    assert [c["feature_id"] for c in ordered] == ["credit_score", "geo_proximity", "recency"]
    again = order_feature_contributions(list(reversed(contribs)), spec.explanations)
    assert ordered == again


def test_recommended_actions_deterministic() -> None:
    spec = _spec()
    a = recommended_actions_for(spec=spec, missing_evidence=["facility.capacity.available_lbs_month"], eligible=False)
    b = recommended_actions_for(spec=spec, missing_evidence=["facility.capacity.available_lbs_month"], eligible=False)
    assert a == b
    assert a
