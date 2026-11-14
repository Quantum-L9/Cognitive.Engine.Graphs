"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, feedback, signal-weights]
owner: engine-team
status: active
--- /L9_META ---

SignalWeightCalculator reads the dimension scores the outcome handler writes (CEG-FEEDBACK-1, FEATURE_GATES §36).

The outcome handler stores ``fingerprint.dimension_scores`` as RU-005 JSON
(``encode_outcome_map``); the calculator counted only ``o.<dim>_score``, which
no engine writer sets. With ``signal_weight_dimension_scores_enabled`` on, the
per-dimension count also matches the dimension's key in the stored JSON. These
tests pin the key against the codec (so a codec change cannot silently break
the match) and the compiled query for both flag states. The Neo4j proof is
``tests/integration/test_feedback_dimension_scores.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from engine.config.settings import settings
from engine.feedback.signal_weights import SignalWeightCalculator, dimension_scores_key
from engine.models.outcomes import encode_outcome_map
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit


def _spec() -> Any:
    return synthetic_loader().load_domain("orbital-logistics")


def test_flag_defaults_off() -> None:
    assert type(settings).model_fields["signal_weight_dimension_scores_enabled"].default is False


def test_key_is_derived_from_the_codec() -> None:
    assert dimension_scores_key("geo_proximity") == '"geo_proximity": '
    assert dimension_scores_key("geo_proximity") in encode_outcome_map({"geo_proximity": 0.9})


@pytest.mark.parametrize(
    ("stored", "dimension", "present"),
    [
        ({"geo_proximity": 0.9}, "geo_proximity", True),
        ({"a": 0.1, "geo_proximity": 0.0, "z": 1.0}, "geo_proximity", True),
        ({"geo_proximity": -3}, "geo_proximity", True),
        ({"other_geo_proximity": 0.9}, "geo_proximity", False),
        ({"geo_proximity_v2": 0.9}, "geo_proximity", False),
        ({"geo": 0.9}, "geo_proximity", False),
        ({}, "geo_proximity", False),
    ],
    ids=["only", "among-others", "negative", "suffix-collision", "prefix-collision", "shorter", "empty"],
)
def test_key_occurs_exactly_when_the_dimension_was_recorded(
    stored: dict[str, float], dimension: str, present: bool
) -> None:
    assert (dimension_scores_key(dimension) in encode_outcome_map(stored)) is present


def test_key_refuses_an_unsanitized_dimension_name() -> None:
    with pytest.raises(ValueError, match="Invalid label"):
        dimension_scores_key('geo": 1, "x')


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_query = AsyncMock(side_effect=self._execute)

    async def _execute(self, cypher: str, parameters: dict[str, Any] | None = None, **_: Any) -> list[dict[str, Any]]:
        self.calls.append((cypher, dict(parameters or {})))
        return [{"dim_total": 4, "dim_wins": 3}]


async def _dimension_query(dimension: str) -> tuple[str, dict[str, Any]]:
    driver = _Driver()
    calculator = SignalWeightCalculator(driver, _spec())  # type: ignore[arg-type]
    await calculator._compute_dimension_weight(dimension, 0.5, 10, "DockingResult")
    assert len(driver.calls) == 1
    return driver.calls[0]


async def test_flag_off_query_is_the_legacy_score_property_only() -> None:
    cypher, parameters = await _dimension_query("depot_reliability")
    assert "WHERE o.tenant = $tenant AND o.depot_reliability_score IS NOT NULL\n" in cypher
    assert "dimension_scores" not in cypher
    assert parameters == {"tenant": "orbital-logistics"}


async def test_flag_on_query_also_matches_the_stored_dimension_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "signal_weight_dimension_scores_enabled", True)
    cypher, parameters = await _dimension_query("depot_reliability")
    assert (
        "WHERE o.tenant = $tenant AND "
        "(o.depot_reliability_score IS NOT NULL OR o.dimension_scores CONTAINS $dimension_key)"
    ) in cypher
    # The key travels as a parameter, never as Cypher text.
    assert '"depot_reliability"' not in cypher
    assert parameters == {"tenant": "orbital-logistics", "dimension_key": '"depot_reliability": '}


async def test_an_unsanitized_dimension_name_never_reaches_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "signal_weight_dimension_scores_enabled", True)
    driver = _Driver()
    calculator = SignalWeightCalculator(driver, _spec())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid label"):
        await calculator._compute_dimension_weight("x_score IS NOT NULL OR true //", 0.5, 10, "DockingResult")
    assert driver.calls == []
