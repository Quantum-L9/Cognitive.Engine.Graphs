"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, feedback]
owner: engine-team
status: active
--- /L9_META ---

Unit — feedback convergence closure (FEEDBACK-PARTIAL-01).

Contract: docs/contracts/feedback/convergence-loop.yaml (blocking_validation:
"failure during propagation must report partial outcome truth").
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.settings import settings
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

_TENANT = "orbital-logistics"


def _spec(*, feedback_enabled: bool = False) -> Any:
    spec = synthetic_loader().load_domain(_TENANT)
    return spec.model_copy(update={"feedbackloop": spec.feedbackloop.model_copy(update={"enabled": feedback_enabled})})


# ── FEEDBACK-PARTIAL-01 ───────────────────────────────────────────────────


class TestPartialResults:
    @staticmethod
    def _loop(*, propagation: Any = None, recalc: Any = False, drift: Any = None) -> Any:
        from engine.feedback.convergence import ConvergenceLoop

        loop = ConvergenceLoop(MagicMock(), _spec(feedback_enabled=True))

        def _mock(value: Any) -> AsyncMock:
            return AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)

        loop._score_propagator.propagate_outcome = _mock(propagation or {"propagated": True})
        loop._weight_calculator.should_recalculate = _mock(recalc)
        loop._weight_calculator.recalculate_weights = AsyncMock(return_value={"fit": 1.1})
        loop._drift_detector.check_and_alert = _mock(drift or {"drift_detected": False})
        return loop

    _OUTCOME = {"outcome_id": "o-1", "outcome": "success", "candidate_id": "c-1"}

    async def test_all_stages_ok_is_complete(self) -> None:
        metadata = await self._loop(recalc=True).on_outcome_recorded(self._OUTCOME)
        assert metadata["status"] == "complete"
        assert metadata["stages"] == {"propagation": "ok", "weights": "ok", "drift": "ok"}
        assert metadata["weights_recalculated"] is True
        assert metadata["new_weights"] == {"fit": 1.1}

    async def test_propagation_failure_is_partial_and_later_stages_still_run(self) -> None:
        loop = self._loop(propagation=RuntimeError("neo4j said no: MATCH (secret)"))
        metadata = await loop.on_outcome_recorded(self._OUTCOME)
        assert metadata["status"] == "partial"
        assert metadata["stages"] == {"propagation": "failed:RuntimeError", "weights": "ok", "drift": "ok"}
        assert "propagation" not in metadata
        assert "secret" not in repr(metadata)
        loop._drift_detector.check_and_alert.assert_awaited_once()

    async def test_weight_and_drift_failures_are_reported_per_stage(self) -> None:
        metadata = await self._loop(recalc=ValueError("x"), drift=TimeoutError("y")).on_outcome_recorded(self._OUTCOME)
        assert metadata["status"] == "partial"
        assert metadata["stages"]["weights"] == "failed:ValueError"
        assert metadata["stages"]["drift"] == "failed:TimeoutError"
        assert metadata["weights_recalculated"] is False

    async def test_flag_off_propagates_the_first_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "feedback_partial_results_enabled", False)
        loop = self._loop(propagation=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await loop.on_outcome_recorded(self._OUTCOME)
