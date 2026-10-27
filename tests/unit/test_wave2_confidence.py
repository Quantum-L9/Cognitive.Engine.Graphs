"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, confidence, wave2]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for W2-03: Ensemble Confidence Bounds Checking.
Tests monoculture detection with various dimension score distributions.
"""

from __future__ import annotations

import pytest

from engine.scoring.confidence import (
    FLAG_ENSEMBLE_DIVERGENCE,
    FLAG_MONOCULTURE,
    ConfidenceChecker,
)


@pytest.mark.unit
class TestMonocultureDetection:
    """Test ConfidenceChecker.check_monoculture()."""

    def test_no_monoculture(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.3, "structural": 0.3, "freshness": 0.2, "reinforcement": 0.2}},
        ]
        flags = checker.check_monoculture(candidates)
        assert flags == []

    def test_monoculture_detected(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.9, "structural": 0.05, "freshness": 0.03, "reinforcement": 0.02}},
        ]
        flags = checker.check_monoculture(candidates)
        assert len(flags) == 1
        assert flags[0]["flag"] == FLAG_MONOCULTURE
        assert flags[0]["dominant_dimension"] == "geo"
        assert flags[0]["contribution"] > 0.70

    def test_exactly_at_threshold_not_flagged(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.70, "structural": 0.30}},
        ]
        flags = checker.check_monoculture(candidates)
        assert flags == []

    def test_just_above_threshold_flagged(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.71, "structural": 0.29}},
        ]
        flags = checker.check_monoculture(candidates)
        assert len(flags) == 1

    def test_multiple_candidates_mixed(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.5, "structural": 0.5}},  # no flag
            {"dimension_scores": {"geo": 0.95, "structural": 0.05}},  # flagged
            {"dimension_scores": {"geo": 0.4, "structural": 0.6}},  # no flag
        ]
        flags = checker.check_monoculture(candidates)
        assert len(flags) == 1
        assert flags[0]["candidate_index"] == 1

    def test_empty_candidates(self):
        checker = ConfidenceChecker()
        flags = checker.check_monoculture([])
        assert flags == []

    def test_missing_dimension_scores(self):
        checker = ConfidenceChecker()
        candidates = [{"name": "no_scores"}]
        flags = checker.check_monoculture(candidates)
        assert flags == []

    def test_zero_total_score_skipped(self):
        checker = ConfidenceChecker()
        candidates = [{"dimension_scores": {"geo": 0.0, "structural": 0.0}}]
        flags = checker.check_monoculture(candidates)
        assert flags == []

    def test_single_dimension(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [{"dimension_scores": {"geo": 0.8}}]
        flags = checker.check_monoculture(candidates)
        # Single dimension = 100% contribution, should be flagged
        assert len(flags) == 1
        assert flags[0]["contribution"] == pytest.approx(1.0, abs=0.001)

    def test_custom_threshold(self):
        checker = ConfidenceChecker(monoculture_threshold=0.50)
        candidates = [
            {"dimension_scores": {"geo": 0.6, "structural": 0.4}},
        ]
        flags = checker.check_monoculture(candidates)
        assert len(flags) == 1  # 0.6/1.0 = 0.6 > 0.5


@pytest.mark.unit
class TestEnsembleDivergence:
    """Test ConfidenceChecker.check_ensemble_divergence() (Wave 6 stub)."""

    def test_no_divergence(self):
        checker = ConfidenceChecker(ensemble_max_divergence=0.30)
        flags = checker.check_ensemble_divergence([0.5, 0.6, 0.7], [0.55, 0.62, 0.68])
        assert flags == []

    def test_divergence_detected(self):
        checker = ConfidenceChecker(ensemble_max_divergence=0.30)
        flags = checker.check_ensemble_divergence([0.2, 0.5], [0.9, 0.5])
        assert len(flags) == 1
        assert flags[0]["flag"] == FLAG_ENSEMBLE_DIVERGENCE
        assert flags[0]["candidate_index"] == 0
        assert flags[0]["divergence"] > 0.30

    def test_empty_scores(self):
        checker = ConfidenceChecker()
        flags = checker.check_ensemble_divergence([], [])
        assert flags == []


@pytest.mark.unit
class TestAnnotateCandidates:
    """Test ConfidenceChecker.annotate_candidates()."""

    def test_annotates_monoculture_flag(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.95, "structural": 0.05}},
            {"dimension_scores": {"geo": 0.5, "structural": 0.5}},
        ]
        result = checker.annotate_candidates(candidates)

        flag = result[0]["confidence_flag"]
        if isinstance(flag, list):
            assert FLAG_MONOCULTURE in flag
        else:
            assert flag == FLAG_MONOCULTURE
        assert result[0]["confidence_detail"]["dominant_dimension"] == "geo"
        assert "confidence_flag" not in result[1]

    def test_no_annotations_when_clean(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.4, "structural": 0.6}},
        ]
        result = checker.annotate_candidates(candidates)

        assert "confidence_flag" not in result[0]

    def test_mutates_in_place(self):
        checker = ConfidenceChecker(monoculture_threshold=0.70)
        candidates = [
            {"dimension_scores": {"geo": 0.95, "structural": 0.05}},
        ]
        result = checker.annotate_candidates(candidates)

        # Should be the same list object
        assert result is candidates


# ── RU-032 / CEG-COR-167: handler feeds weighted contributions to monoculture ──


class _DimensionRowsDriver:
    """Fake GraphDriver returning rows shaped like handle_match's RETURN."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.cyphers: list[str] = []

    async def execute_query(self, *, cypher: str, parameters: dict, database: str) -> list[dict]:
        self.cyphers.append(cypher)
        return [{k: (dict(v) if isinstance(v, dict) else v) for k, v in r.items()} for r in self.rows]


def _weighted_spec():
    from tests.fixtures.domain_contracts import synthetic_loader

    spec = synthetic_loader().load_domain("orbital-logistics")
    base = spec.scoring.dimensions[0]
    dims = [
        base.model_copy(update={"name": "tiny", "weightkey": "w_tiny", "defaultweight": 0.05}),
        base.model_copy(update={"name": "a", "weightkey": "w_a", "defaultweight": 0.45}),
        base.model_copy(update={"name": "b", "weightkey": "w_b", "defaultweight": 0.5}),
    ]
    return spec.model_copy(update={"scoring": spec.scoring.model_copy(update={"dimensions": dims})})


async def _match_one_row(values: dict[str, float]) -> dict:
    from unittest.mock import MagicMock

    from engine.handlers import handle_match, init_dependencies

    row = {"candidate": {"depot_id": "x1"}, "score": 0.24, "dimension_scores": values}
    loader = MagicMock()
    loader.load_domain.return_value = _weighted_spec()
    init_dependencies(_DimensionRowsDriver([row]), loader)  # type: ignore[arg-type]
    result = await handle_match("orbital-logistics", {"query": {}, "match_direction": "window_to_depot"})
    return result["candidates"][0]


def _flags(candidate: dict) -> list[str]:
    flag = candidate.get("confidence_flag")
    if flag is None:
        return []
    return flag if isinstance(flag, list) else [flag]


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-167")
class TestHandlerMonocultureUsesWeightedContributions:
    VALUES = {"tiny": 1.0, "a": 0.2, "b": 0.2}

    async def test_low_weight_high_value_dimension_is_not_monoculture(self) -> None:
        cand = await _match_one_row(dict(self.VALUES))
        assert FLAG_MONOCULTURE not in _flags(cand)  # weighted share of 'tiny' = 0.208

    async def test_row_dimension_scores_are_not_overwritten(self) -> None:
        cand = await _match_one_row(dict(self.VALUES))
        assert cand["dimension_scores"] == self.VALUES

    async def test_dimensional_agreement_still_reads_raw_values(self) -> None:
        from engine.scoring.confidence import FLAG_LOW_DIMENSIONAL_AGREEMENT

        cand = await _match_one_row(dict(self.VALUES))
        assert FLAG_LOW_DIMENSIONAL_AGREEMENT in _flags(cand)

    async def test_flag_off_restores_raw_value_monoculture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "monoculture_weighted_contribution", False)
        cand = await _match_one_row(dict(self.VALUES))
        assert FLAG_MONOCULTURE in _flags(cand)
        assert cand["confidence_detail"]["contribution"] == pytest.approx(0.7143)

    def test_assembler_exposes_the_emitted_coefficients(self) -> None:
        from engine.scoring.assembler import ScoringAssembler

        assembler = ScoringAssembler(_weighted_spec())
        clause, _ = assembler.assemble_scoring_clause("window_to_depot", {"w_a": 0.4})
        assert assembler.last_effective_weights == {"tiny": 0.05, "a": 0.4, "b": 0.5}
        for name, weight in assembler.last_effective_weights.items():
            assert f"({weight} * {name})" in clause
