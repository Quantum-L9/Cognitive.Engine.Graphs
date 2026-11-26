"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, match, weights, validation]
owner: engine-team
status: active
--- /L9_META ---

SC-003 (request part) / SC-004 (campaign ceg-scoring-audit, lane L3, D3).

A request weight is a finite number in [0, 1]. Before this lane the check ran
only while ``score_clamp_enabled`` was on, coerced with ``float()`` (so ``"0.5"``
and ``True`` passed), and on the legacy path let ``nan`` through (``nan < 0`` and
``nan > 1`` are both false). Under ``match_request_weight_strict_enabled`` every
malformed weight is refused with ``ValidationError`` whatever the clamp flag
and whichever validation path (effective or supplied keys) is active, and no
query is sent.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.settings import settings
from engine.handlers import ValidationError, handle_match, init_dependencies
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

DOMAIN = "orbital-logistics"
DIRECTION = "window_to_depot"
KEY = "w_reliability"  # declared weightkey of depot_reliability

MALFORMED: list[tuple[str, Any]] = [
    ("string", "0.5"),
    ("bool_true", True),
    ("bool_false", False),
    ("list", [0.1]),
    ("none", None),
    ("inf", math.inf),
    ("neg_inf", -math.inf),
    ("nan", math.nan),
    ("negative", -0.1),
    ("above_one", 1.5),
    ("huge_int", 10**400),  # float() overflows; must still be a ValidationError
    ("int_above_one", 2),
]


class Driver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append(cypher)
        return []


def _wire() -> Driver:
    driver = Driver()
    loader = MagicMock()
    loader.load_domain.return_value = synthetic_loader().load_domain(DOMAIN)
    loader.list_domains.return_value = [DOMAIN]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver


async def _match(weights: Any) -> dict[str, Any]:
    return await handle_match(DOMAIN, {"query": {}, "match_direction": DIRECTION, "top_n": 3, "weights": weights})


@pytest.fixture(params=[True, False], ids=["clamp_on", "clamp_off"])
def clamp(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(settings, "score_clamp_enabled", request.param)
    return bool(request.param)


@pytest.fixture(params=[True, False], ids=["effective", "supplied_keys"])
def effective(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(settings, "match_effective_weight_validation", request.param)
    return bool(request.param)


def test_flag_ships_on() -> None:
    assert settings.match_request_weight_strict_enabled is True


@pytest.mark.parametrize(("label", "value"), MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_malformed_weight_is_refused(label: str, value: Any, clamp: bool, effective: bool) -> None:
    driver = _wire()
    with pytest.raises(ValidationError) as excinfo:
        await _match({KEY: value})
    assert KEY in str(excinfo.value)
    assert driver.calls == []  # refused before any query


@pytest.mark.parametrize("weights", [[0.1], "w_reliability=0.5", 0.5], ids=["list", "string", "number"])
async def test_weights_that_are_not_a_mapping_are_refused(weights: Any, clamp: bool) -> None:
    driver = _wire()
    with pytest.raises(ValidationError):
        await _match(weights)
    assert driver.calls == []


@pytest.mark.parametrize("value", [0, 0.0, 0.3, 1, 1.0], ids=["int0", "zero", "mid", "int1", "one"])
async def test_valid_weight_is_accepted(value: float, monkeypatch: pytest.MonkeyPatch, clamp: bool) -> None:
    driver = _wire()
    # Keep the effective sum within the 1.0 ceiling: the other declared weight is 0.3.
    result = await _match({KEY: value, "w_ports": 0.0})
    assert result["status"] == "success"
    assert len(driver.calls) == 1


async def test_sum_ceiling_holds_without_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_clamp_enabled", False)
    _wire()
    with pytest.raises(ValidationError):
        await _match({KEY: 0.8, "w_ports": 0.8})


async def test_empty_and_absent_weights_are_accepted(clamp: bool) -> None:
    for weights in ({}, None):
        driver = _wire()
        result = await _match(weights)
        assert result["status"] == "success"
        assert len(driver.calls) == 1


class TestRollback:
    async def test_flag_off_restores_clamp_gated_coercion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "match_request_weight_strict_enabled", False)
        monkeypatch.setattr(settings, "score_clamp_enabled", False)
        driver = _wire()
        result = await _match({KEY: -0.5})  # legacy: unchecked without the clamp
        assert result["status"] == "success"
        assert len(driver.calls) == 1

    async def test_flag_off_with_clamp_keeps_float_coercion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "match_request_weight_strict_enabled", False)
        monkeypatch.setattr(settings, "score_clamp_enabled", True)
        _wire()
        result = await _match({KEY: "0.5"})
        assert result["status"] == "success"
