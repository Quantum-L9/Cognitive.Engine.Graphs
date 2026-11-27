"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, numeric, lifecycle]
owner: engine-team
status: active
--- /L9_META ---

G02-SCORE-NUMERIC and G03-SCORE-LIFECYCLE at the G01-SCORE-CENSUS pin.

Each reachable module is exercised in the flag or admin state that actually
calls it. Null, zero and NaN are distinct: zero is a score, null and NaN are
not. Opt-in routes stay denied at their default-off flags. Dormant modules are
recorded as having no production caller and are not imported.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engine.config.schema import CalibrationPair, ComputationType, DecisionArbitrationSpec
from engine.config.settings import settings
from engine.handlers import (
    FeatureNotEnabled,
    ValidationError,
    handle_admin,
    handle_match,
    handle_score_remote,
    init_dependencies,
)
from engine.scoring.assembler import ScoringAssembler
from engine.scoring.calibration import ScoreCalibrator, score_distribution_drift
from engine.scoring.evidence_priority import (
    EvidenceObservation,
    EvidenceRead,
    ScoreRequest,
    approved_relevance_map,
    score_cohort,
)
from engine.scoring.feedback import OutcomeFeedback
from engine.scoring.graph_evidence import _observation
from engine.scoring.hgkr_utils import (
    check_score_drift,
    compute_adaptive_sample_size,
    generate_calibration_pairs,
    generate_density_report,
)
from engine.scoring.pareto import ParetoCandidate, compute_pareto_front
from engine.scoring.pareto_integrator import apply_pareto_filter
from engine.scoring.weight_discovery import adaptive_weight_discovery
from engine.state import get_state
from tests.fixtures.domain_contracts import synthetic_loader
from tests.fixtures.orbital_match import DIRECTION, ORBITAL

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
IMPORTANCE_BLOB = "4c94c36ee037470429dd2e1f728f2d3b486b9373"
DORMANT_MODULES = (
    "engine.scoring.belief_propagation",
    "engine.scoring.importance",
    "engine.scoring.helpfulness",
)
REL = "TENDED_BY"


class RecordingDriver:
    """Returns canned rows and records every read. Not a Neo4j stand-in."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[str] = []

    async def execute_query(self, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append(database)
        return copy.deepcopy(self.rows)


class FixedLoader:
    def __init__(self, spec: Any) -> None:
        self.spec = spec

    def load_domain(self, _tenant: str) -> Any:
        return self.spec


def _spec() -> Any:
    return synthetic_loader().load_domain(ORBITAL)


def _wire(rows: list[dict[str, Any]] | None = None) -> RecordingDriver:
    driver = RecordingDriver(rows)
    get_state().reset()
    init_dependencies(driver, FixedLoader(_spec()))
    return driver


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


def _row(depot_id: str, score: float, dims: dict[str, float]) -> dict[str, Any]:
    return {
        "candidate": {
            "depot_id": depot_id,
            "accepts_hazmat": True,
            "docking_ports": 4,
            "reliability": 0.4,
        },
        "score": score,
        "dimension_scores": dims,
    }


def _production_imports() -> set[str]:
    found: set[str] = set()
    for base in (ROOT / "engine", ROOT / "chassis"):
        for path in base.rglob("*.py"):
            if any(path.name == f"{name}.py" for name in ("belief_propagation", "importance", "helpfulness")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
    return found


def test_dormant_modules_have_no_production_caller_and_are_not_imported() -> None:
    """DORMANT_INTERNAL: evidence only. This test does not call the modules."""
    imports = _production_imports()
    for module in DORMANT_MODULES:
        assert module not in imports
    names = {member.value for member in ComputationType}
    assert "importance" not in names
    assert "helpfulness" not in names
    assert "belief" not in names
    payload = (ROOT / "engine/scoring/importance.py").read_bytes()
    header = f"blob {len(payload)}\0".encode()
    # Git blob ids are SHA-1 by definition; this is an identity check, not a security digest.
    digest = hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()
    assert digest == IMPORTANCE_BLOB


def test_assembler_null_clause_clamps_null_and_keeps_a_zero_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_clamp_enabled", True)
    assembler = ScoringAssembler(_spec())
    clause, _meta = assembler.assemble_scoring_clause(DIRECTION, {"w_ports": 0.0})
    assert "WHEN (log(1 + coalesce(candidate.docking_ports, 0)) / log(1 + 1000.0)) IS NULL THEN 0.0" in clause
    assert "isNaN" in clause
    assert "(0.0 * port_capacity)" in clause
    # Independent arithmetic: a zero property is log(1+0)/log(1+1000) = 0, not a missing score.
    assert math.log(1 + 0) / math.log(1 + 1000.0) == 0.0


@pytest.mark.asyncio
async def test_assembler_null_and_nan_weights_are_denied_before_the_query() -> None:
    driver = _wire()
    with pytest.raises(ValidationError, match="not a valid number"):
        await handle_match(
            ORBITAL,
            {"query": {"min_ports": 1}, "match_direction": DIRECTION, "weights": {"w_ports": None}},
        )
    assert driver.calls == []
    with pytest.raises(ValidationError, match="not a finite number"):
        await handle_match(
            ORBITAL,
            {
                "query": {"min_ports": 1},
                "match_direction": DIRECTION,
                "weights": {"w_reliability": float("nan")},
            },
        )
    assert driver.calls == []


@pytest.mark.asyncio
async def test_confidence_and_pareto_reach_match_for_null_zero_and_nan(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.confidence_check_enabled is True
    assert settings.pareto_enabled is True
    rows = [
        _row("tied-a", 0.0, {"depot_reliability": 0.0, "port_capacity": 0.0}),
        _row("tied-b", 0.0, {"depot_reliability": 0.0, "port_capacity": 0.0}),
        _row("finite", 0.4, {"depot_reliability": 0.4, "port_capacity": 0.2}),
        _row("missing", 0.2, {"depot_reliability": 0.2}),
        _row("poison", 0.2, {"depot_reliability": 0.4, "port_capacity": float("nan")}),
    ]
    _wire(rows)
    result = await handle_match(ORBITAL, {"query": {"min_ports": 1}, "match_direction": DIRECTION, "top_n": 10})
    by_id = {row["candidate"]["depot_id"]: row for row in result["candidates"]}
    # Zero is a score: two all-zero depots tie on the front with each other, and the
    # finite depot dominates the NaN row rather than sharing a front with it.
    front = set(result["pareto"]["non_dominated_ids"])
    assert front == {"finite"}
    assert by_id["poison"]["confidence_flag"] == ["monoculture"]
    # A null component is not a contribution. The remaining finite one is the whole score.
    assert by_id["missing"].get("confidence_flag") == ["monoculture"]

    # SCR-13 / L10 rollback: flag off restores the bare-string shape for one flag.
    monkeypatch.setattr(settings, "confidence_flag_list_enabled", False)
    legacy = await handle_match(ORBITAL, {"query": {"min_ports": 1}, "match_direction": DIRECTION, "top_n": 10})
    legacy_by_id = {row["candidate"]["depot_id"]: row for row in legacy["candidates"]}
    assert legacy_by_id["poison"]["confidence_flag"] == "monoculture"
    assert legacy_by_id["missing"].get("confidence_flag") == "monoculture"
    monkeypatch.setattr(settings, "confidence_flag_list_enabled", True)

    monkeypatch.setattr(settings, "pareto_enabled", False)
    monkeypatch.setattr(settings, "confidence_check_enabled", False)
    denied = await handle_match(ORBITAL, {"query": {"min_ports": 1}, "match_direction": DIRECTION, "top_n": 10})
    assert "pareto" not in denied
    assert all("confidence_flag" not in row for row in denied["candidates"])


def test_pareto_nan_loses_to_a_finite_score_and_zero_ties() -> None:
    finite = ParetoCandidate("finite", {"quality": 0.4})
    poisoned = ParetoCandidate("poison", {"quality": float("nan")})
    front = compute_pareto_front([finite, poisoned])
    assert [c.candidate_id for c in front.non_dominated] == ["finite"]
    zeros = compute_pareto_front(
        [
            ParetoCandidate("a", {"quality": 0.0}),
            ParetoCandidate("b", {"quality": 0.0}),
        ]
    )
    assert zeros.front_size == 2


@pytest.mark.asyncio
async def test_remote_score_default_off_denies_evidence_and_graph_before_deps() -> None:
    assert settings.remote_score_enabled is False
    get_state().reset()
    with pytest.raises(FeatureNotEnabled, match="REMOTE_SCORE_ENABLED"):
        await handle_score_remote(ORBITAL, {"entity_refs": ["res.partner:1"], "top_n": 1})


def test_evidence_priority_null_zero_and_nan_do_not_score() -> None:
    request = ScoreRequest(entity_refs=("res.partner:1", "res.partner:2", "res.partner:3"), top_n=3)
    relevance = approved_relevance_map({REL: 1.0})

    def observation(ref: str, confidence: float | None) -> EvidenceObservation:
        return EvidenceObservation(
            entity_ref=ref,
            evidence_ref=f"evidence:{ref}",
            relationship_type=REL,
            source_ref="source:1",
            confidence=confidence,
            admission="admitted",
            polarity="positive",
        )

    read = EvidenceRead(
        connected=False,
        database=ORBITAL,
        observations=(
            observation("res.partner:1", None),
            observation("res.partner:2", 0.0),
            observation("res.partner:3", float("nan")),
        ),
    )
    result = score_cohort(request, read, relevance, identity_per_type=True)
    assert result["scored"] == []
    assert {row["entity_ref"] for row in result["abstained"]} == {"res.partner:1", "res.partner:2", "res.partner:3"}


def test_graph_evidence_null_confidence_is_not_a_qualifying_observation() -> None:
    parsed = _observation(
        {
            "entity_ref": "res.partner:9",
            "relationship_type": REL,
            "props": {
                "evidence_ref": "evidence:9",
                "source_ref": "source:9",
                "confidence": None,
                "assertion_status": "observed",
                "polarity": "positive",
            },
        },
        datetime.now(tz=UTC),
    )
    assert parsed is not None
    assert parsed.confidence is None
    request = ScoreRequest(entity_refs=("res.partner:9",), top_n=1)
    result = score_cohort(
        request,
        EvidenceRead(connected=True, database=ORBITAL, observations=(parsed,)),
        approved_relevance_map({REL: 1.0}),
        identity_per_type=True,
    )
    assert result["scored"] == []
    assert result["abstained"][0]["score"] is None


@pytest.mark.asyncio
async def test_feedback_default_off_denies_and_nan_does_not_propose_a_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.handlers import _feedback_effective
    from tests.fixtures.orbital_match import with_feedback_loop

    driver = _wire()
    denied = await handle_admin(ORBITAL, {"subaction": "score_feedback"})
    assert denied["status"] == "disabled"
    assert driver.calls == []
    assert _feedback_effective(with_feedback_loop(_spec())) is False

    monkeypatch.setattr(settings, "feedback_enabled", True)
    assert _feedback_effective(with_feedback_loop(_spec())) is True
    outcomes: list[dict[str, Any]] = []
    for index in range(6):
        outcomes.append({"outcome": "success", "dimension_scores": {"quality": 0.8}})
        outcomes.append({"outcome": "failure", "dimension_scores": {"quality": 0.0}})
    outcomes.append({"outcome": "success", "dimension_scores": {"quality": None}})
    outcomes.append({"outcome": "failure", "dimension_scores": {"quality": float("nan")}})
    report = OutcomeFeedback(outcomes).compute_feedback()
    assert report["sufficient_data"] is True
    assert math.isfinite(report["proposed_weights"]["quality"])
    assert report["dimension_analysis"]["quality"]["negative_mean"] == 0.0


def test_calibration_null_fails_closed_zero_passes_and_nan_is_not_a_distribution() -> None:
    spec = _spec()
    pair = CalibrationPair(node_a="m", node_b="c", expected_score_min=0.0, expected_score_max=0.2, label="zero")
    report = ScoreCalibrator(spec).run_calibration([pair], {("m", "c"): 0.0})
    assert report.overall_pass is True
    missing = ScoreCalibrator(spec).run_calibration([pair], {})
    assert missing.results[0].passed is False
    assert missing.results[0].actual_score is None
    poisoned = ScoreCalibrator(spec).run_calibration([pair], {("m", "c"): float("nan")})
    assert poisoned.results[0].passed is False
    empty = score_distribution_drift([], [0.0])
    assert empty.status == "insufficient_data"
    assert empty.drift_detected is False
    zeros = score_distribution_drift([0.0, 0.0], [0.0, 0.0])
    assert zeros.drift_detected is False
    assert zeros.delta_mean == 0.0
    hidden = score_distribution_drift([0.0, 0.0], [1.0, float("nan")])
    assert hidden.drift_detected is True
    assert math.isfinite(hidden.delta_mean)


@pytest.mark.asyncio
async def test_calibration_admin_reaches_the_module_and_match_does_not() -> None:
    driver = _wire()
    result = await handle_admin(ORBITAL, {"subaction": "calibration_run", "domain_id": ORBITAL})
    assert result["subaction"] == "calibration_run"
    assert result["total_pairs"] == 0
    assert driver.calls == []
    assert "ScoreCalibrator" not in handle_match.__code__.co_names


def test_hgkr_null_zero_and_nan_pairs_and_drift() -> None:
    pairs = generate_calibration_pairs(
        [
            {"match_id": "m0", "candidate_id": "c0", "outcome": "success", "score": 0.0},
            {"match_id": "mn", "candidate_id": "cn", "outcome": "success", "score": None},
            {"match_id": "mq", "candidate_id": "cq", "outcome": "failure", "score": float("nan")},
        ]
    )
    assert len(pairs) == 1
    assert pairs[0].expected_score_min == 0.0
    assert compute_adaptive_sample_size(0.0) == 16
    with pytest.raises(ValueError):
        compute_adaptive_sample_size(float("nan"))
    density = generate_density_report(ORBITAL, 0, 0)
    assert density.avg_degree == 0.0
    drift = check_score_drift([0.0], [float("nan")], "depot_recency")
    assert drift.drift_detected is False
    shifted = check_score_drift([0.0, 0.0], [0.2, float("nan")], "depot_recency", threshold=0.05)
    assert shifted.drift_detected is True
    assert math.isfinite(shifted.delta_mean)


@pytest.mark.asyncio
async def test_hgkr_admin_reaches_pair_generation_and_match_does_not() -> None:
    driver = _wire(
        [
            {"match_id": "m0", "candidate_id": "c0", "outcome": "success", "score": 0.0},
            {"match_id": "mn", "candidate_id": "cn", "outcome": "success", "score": None},
        ]
    )
    result = await handle_admin(ORBITAL, {"subaction": "generate_calibration_pairs", "domain_id": ORBITAL})
    assert result["subaction"] == "generate_calibration_pairs"
    assert result["total_pairs"] == 1
    assert driver.calls == [ORBITAL]
    assert "generate_calibration_pairs" not in handle_match.__code__.co_names


def test_pareto_integrator_zero_is_kept_and_nan_is_dominated() -> None:
    assert DecisionArbitrationSpec().enabled is False
    assert settings.pareto_precut_enabled is False
    filtered = apply_pareto_filter(
        [
            {"candidate": {"entity_id": "zero"}, "dimension_scores": {"quality": 0.0}, "score": 0.0},
            {"candidate": {"entity_id": "finite"}, "dimension_scores": {"quality": 0.4}, "score": 0.4},
            {"candidate": {"entity_id": "poison"}, "dimension_scores": {"quality": float("nan")}, "score": 0.9},
        ],
        ["quality"],
    )
    assert filtered["nondominated"] == ["finite"]
    assert set(filtered["dominated"]) == {"zero", "poison"}


@pytest.mark.asyncio
async def test_weight_discovery_default_off_and_null_history_stays_finite(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.pareto_weight_discovery_enabled is False
    driver = _wire()
    with pytest.raises(FeatureNotEnabled, match="PARETO_WEIGHT_DISCOVERY_ENABLED"):
        await handle_admin(ORBITAL, {"subaction": "discover_weights", "domain_id": ORBITAL})
    assert driver.calls == []

    monkeypatch.setattr(settings, "pareto_weight_discovery_enabled", True)
    vectors = await adaptive_weight_discovery(
        ["quality"],
        [
            {"dimension_scores": {"quality": 0.0}, "was_selected": True},
            {"dimension_scores": {"quality": None}, "was_selected": False},
            {"dimension_scores": {"quality": float("nan")}, "was_selected": True},
        ],
        n_samples=2,
    )
    assert vectors
    assert all(math.isfinite(vector.ndcg_score) for vector in vectors)
