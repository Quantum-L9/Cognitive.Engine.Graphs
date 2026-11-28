"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, confidence, contract]
owner: engine-team
status: active
--- /L9_META ---

CEG-11 (fourth external audit, 2026-11-27): the native ``MatchCandidate``
model, the draft ``match-response.schema.yaml`` and both W2-03 producers agree
on ``confidence_flag: list[str]``.

The published contract is array-only. ``confidence_flag_list_enabled=False``
restores the pre-contract legacy shape (absent / bare string / pair); that
rollback is outside the contract, and both the schema and the model reject a
bare string.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from engine.config.settings import settings
from engine.handlers import _annotate_weighted_confidence, handle_match, init_dependencies
from engine.models.payloads import MatchCandidate
from engine.scoring.confidence import (
    FLAG_LOW_DIMENSIONAL_AGREEMENT,
    FLAG_MONOCULTURE,
    ConfidenceChecker,
)
from tests.fixtures.domain_contracts import synthetic_loader
from tests.fixtures.match_response_contract import draft_response_validator

pytestmark = pytest.mark.unit

_EXAMPLE = Path(__file__).resolve().parents[2] / "contracts" / "payloads" / "examples" / "match-response.json"
_WEIGHTS = {"geo": 0.5, "structural": 0.5}
_ROWS = (
    {"geo": 0.4, "structural": 0.6},  # no flag
    {"geo": 0.85, "structural": 0.35},  # monoculture
    {"geo": 0.95, "structural": 0.05},  # monoculture + low agreement
)


def _example() -> dict[str, Any]:
    return json.loads(_EXAMPLE.read_text(encoding="utf-8"))


def _candidate() -> dict[str, Any]:
    return copy.deepcopy(_example()["candidates"][0])


def _schema_errors(candidate: dict[str, Any]) -> list[str]:
    response = _example()
    response["candidates"] = [candidate]
    return [e.message for e in draft_response_validator().iter_errors(response)]


def _produce(weighted: bool) -> list[Any]:
    rows = [{"dimension_scores": dict(d)} for d in _ROWS]
    checker = ConfidenceChecker(monoculture_threshold=0.70)
    if weighted:
        _annotate_weighted_confidence(checker, rows, _WEIGHTS)
    else:
        checker.annotate_candidates(rows)
    return [row.get("confidence_flag") for row in rows]


class _Driver:
    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        dims = ({"depot_reliability": 0.5, "port_capacity": 0.5}, {"depot_reliability": 0.95, "port_capacity": 0.16})
        return [
            {"candidate": {"depot_id": f"d{i}"}, "score": 0.5, "dimension_scores": dict(d), "absent_sources": []}
            for i, d in enumerate(dims)
        ]


async def _match_rows() -> list[dict[str, Any]]:
    loader = MagicMock()
    loader.load_domain.return_value = synthetic_loader().load_domain("orbital-logistics")
    loader.list_domains.return_value = ["orbital-logistics"]
    init_dependencies(_Driver(), loader)  # type: ignore[arg-type]
    result = await handle_match("orbital-logistics", {"query": {}, "match_direction": "window_to_depot", "top_n": 10})
    return list(result["candidates"])


class TestNativeModel:
    def test_absent_flag_stays_absent(self) -> None:
        """CEG-16: absent means the confidence stage did not run; it is never defaulted to []."""
        model = MatchCandidate.model_validate(_candidate())
        assert model.confidence_flag is None
        assert "confidence_flag" not in model.model_dump()
        assert "confidence_flag" not in json.loads(model.model_dump_json())

    def test_explicit_none_is_absent_on_serialization(self) -> None:
        model = MatchCandidate.model_validate(_candidate() | {"confidence_flag": None})
        assert "confidence_flag" not in model.model_dump()

    def test_empty_list_round_trips_as_ran_clean(self) -> None:
        model = MatchCandidate.model_validate(_candidate() | {"confidence_flag": []})
        assert model.model_dump()["confidence_flag"] == []
        assert json.loads(model.model_dump_json())["confidence_flag"] == []

    @pytest.mark.parametrize(
        "flags",
        [[], [FLAG_MONOCULTURE], [FLAG_MONOCULTURE, FLAG_LOW_DIMENSIONAL_AGREEMENT]],
        ids=["none", "one", "two"],
    )
    def test_list_values_validate(self, flags: list[str]) -> None:
        cand = _candidate() | {"confidence_flag": flags}
        assert MatchCandidate.model_validate(cand).confidence_flag == flags

    @pytest.mark.parametrize(
        "bad",
        [FLAG_MONOCULTURE, ["divergent"], [FLAG_MONOCULTURE, FLAG_MONOCULTURE]],
        ids=["legacy-string", "unknown-flag", "duplicate"],
    )
    def test_off_contract_values_are_rejected(self, bad: Any) -> None:
        with pytest.raises(ValidationError):
            MatchCandidate.model_validate(_candidate() | {"confidence_flag": bad})


class TestProducersAgreeWithContract:
    @pytest.mark.parametrize("weighted", [True, False], ids=["weighted", "raw"])
    def test_default_producer_output_is_valid_for_schema_and_model(self, weighted: bool) -> None:
        assert settings.confidence_flag_list_enabled is True
        produced = _produce(weighted)
        assert produced[0] == []
        assert produced[2] == [FLAG_MONOCULTURE, FLAG_LOW_DIMENSIONAL_AGREEMENT]
        for flags in produced:
            cand = _candidate() | {"confidence_flag": flags}
            assert _schema_errors(cand) == []
            assert MatchCandidate.model_validate(cand).confidence_flag == flags

    def test_both_producers_agree_on_shape(self) -> None:
        assert [type(f) for f in _produce(True)] == [type(f) for f in _produce(False)] == [list, list, list]

    @pytest.mark.parametrize("weighted", [True, False], ids=["weighted", "raw"])
    @pytest.mark.parametrize("stage_on", [True, False], ids=["stage_ran", "stage_off"])
    async def test_three_states_through_handle_match(
        self, weighted: bool, stage_on: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CEG-16: stage ran -> list[str] on every candidate ([] when clean); stage off ->
        absent. Each candidate's value is valid for the native model and the schema."""
        monkeypatch.setattr(settings, "monoculture_weighted_contribution", weighted)
        monkeypatch.setattr(settings, "confidence_check_enabled", stage_on)
        rows = await _match_rows()
        for row in rows:
            cand = _candidate()
            if stage_on:
                assert isinstance(row["confidence_flag"], list)
                cand["confidence_flag"] = row["confidence_flag"]
            else:
                assert "confidence_flag" not in row
            model = MatchCandidate.model_validate(cand)
            assert _schema_errors(cand) == []
            assert ("confidence_flag" in model.model_dump()) is stage_on
        if stage_on:
            assert [] in [row["confidence_flag"] for row in rows]
            assert any(row["confidence_flag"] for row in rows)

    def test_legacy_rollback_shape_is_outside_the_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "confidence_flag_list_enabled", False)
        legacy = _produce(False)[1]
        assert legacy == FLAG_MONOCULTURE
        cand = _candidate() | {"confidence_flag": legacy}
        assert _schema_errors(cand) != []
        with pytest.raises(ValidationError):
            MatchCandidate.model_validate(cand)
