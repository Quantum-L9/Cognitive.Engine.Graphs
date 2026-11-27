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

import pytest
from pydantic import ValidationError

from engine.config.settings import settings
from engine.handlers import _annotate_weighted_confidence
from engine.models.payloads import MatchCandidate
from engine.scoring.confidence import (
    FLAG_LOW_DIMENSIONAL_AGREEMENT,
    FLAG_MONOCULTURE,
    ConfidenceChecker,
)
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


class TestNativeModel:
    def test_absent_flag_defaults_to_empty_list(self) -> None:
        assert MatchCandidate.model_validate(_candidate()).confidence_flag == []

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

    def test_legacy_rollback_shape_is_outside_the_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "confidence_flag_list_enabled", False)
        legacy = _produce(False)[1]
        assert legacy == FLAG_MONOCULTURE
        cand = _candidate() | {"confidence_flag": legacy}
        assert _schema_errors(cand) != []
        with pytest.raises(ValidationError):
            MatchCandidate.model_validate(cand)
