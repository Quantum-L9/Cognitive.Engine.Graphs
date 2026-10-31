"""Unit tests — ScorePropagator applies its factor once per event, bounded (RU-028 / CEG-COR-117, RG-035).

The legacy Cypher ``MATCH (o ...) WITH o MATCH (dw ...) SET ...`` produces
|outcomes| x |DimensionWeight| rows, so SET ran k times per DimensionWeight
(factor^k) and ``candidates_affected`` was k*|dw|, with no bound.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.feedback.score_propagator import ScorePropagator


def _spec(min_weight: float = 0.1, max_weight: float = 3.0) -> MagicMock:
    spec = MagicMock()
    spec.domain.id = "plasticos"
    spec.feedbackloop.outcome_node_label = "TransactionOutcome"
    spec.feedbackloop.signal_weights.min_weight = min_weight
    spec.feedbackloop.signal_weights.max_weight = max_weight
    return spec


def _driver(affected: int = 2) -> MagicMock:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[{"affected": affected}])
    return driver


def _captured(driver: MagicMock) -> tuple[str, dict]:
    args, kwargs = driver.execute_query.call_args
    cypher = kwargs.get("cypher", args[0] if args else "")
    return cypher, kwargs["parameters"]


@pytest.mark.unit
class TestSingleApplication:
    @pytest.mark.asyncio
    async def test_outcome_rows_are_collapsed_before_set(self) -> None:
        driver = _driver()
        await ScorePropagator(driver, _spec()).propagate_outcome({"outcome": "success", "candidate_id": "F-1"})

        cypher, params = _captured(driver)
        flat = " ".join(cypher.split())
        # Existence of the outcome is aggregated to one row before the DimensionWeight MATCH.
        assert re.search(r"WITH count\(o\) AS \w+ WHERE \w+ > 0 MATCH \(dw:DimensionWeight", flat)
        assert "WITH o MATCH (dw" not in flat
        # Clamp to the spec's signal_weights bounds, as parameters.
        assert "$min_weight" in cypher
        assert "$max_weight" in cypher
        assert params["min_weight"] == 0.1
        assert params["max_weight"] == 3.0
        assert params["factor"] == 1.15
        assert params["tenant"] == "plasticos"
        assert params["candidate_id"] == "F-1"
        _, kwargs = driver.execute_query.call_args
        assert kwargs["database"] == "plasticos"

    @pytest.mark.asyncio
    async def test_affected_is_reported_from_the_query(self) -> None:
        driver = _driver(affected=2)
        result = await ScorePropagator(driver, _spec()).propagate_outcome({"outcome": "failure", "candidate_id": "F-1"})
        assert result["candidates_affected"] == 2
        _, params = _captured(driver)
        assert params["factor"] == pytest.approx(1.0 / 1.15)

    @pytest.mark.asyncio
    async def test_partial_outcome_takes_no_action(self) -> None:
        driver = _driver()
        result = await ScorePropagator(driver, _spec()).propagate_outcome({"outcome": "partial", "candidate_id": "F-1"})
        assert result == {"propagated": False, "reason": "partial_outcome_no_action"}
        driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_off_restores_legacy_cypher(self, monkeypatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "propagation_single_application", False)
        driver = _driver()
        await ScorePropagator(driver, _spec()).propagate_outcome({"outcome": "success", "candidate_id": "F-1"})
        cypher, params = _captured(driver)
        assert "WITH o\n        MATCH (dw:DimensionWeight {tenant: $tenant})" in cypher
        assert "min_weight" not in params
