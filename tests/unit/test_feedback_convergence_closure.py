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

Unit — feedback convergence closure (FEEDBACK-IDEMP-01, FEEDBACK-PARTIAL-01).

Contract: docs/contracts/feedback/convergence-loop.yaml (blocking_validation).
The graph semantics (MERGE, write lock, single application) are proven against
Neo4j in tests/integration/test_feedback_convergence_closure.py; these tests pin
the handler and loop control flow with a fake driver.
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


class _Driver:
    """Records every query; answers the keyed outcome write from ``write_rows``."""

    def __init__(self, write_rows: list[dict[str, Any]] | None = None, constraint_error: Exception | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._write_rows = write_rows
        self._constraint_error = constraint_error

    async def execute_query(self, cypher: str, parameters: dict[str, Any] | None = None, **_: Any) -> list[Any]:
        self.calls.append((cypher, dict(parameters or {})))
        if "CREATE CONSTRAINT ceg_outcome_id_" in cypher and self._constraint_error:
            raise self._constraint_error
        if "MERGE (o:" in cypher and self._write_rows is not None:
            return self._write_rows
        if "MERGE (o:" in cypher:
            p = parameters or {}
            return [{"outcome_id": p["outcome_id"], "created": True}]
        return []

    def cyphers(self) -> list[str]:
        return [c for c, _ in self.calls]


def _wire(driver: _Driver, spec: Any) -> None:
    from engine.handlers import init_dependencies
    from engine.state import get_state

    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [_TENANT]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    get_state()._tenant_allowlist = {_TENANT}


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"match_id": "m-1", "candidate_id": "c-1", "outcome": "success"}
    payload.update(overrides)
    return payload


def _stored(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "outcome_id": "replay-1",
        "created": False,
        "stored_match_id": "m-1",
        "stored_candidate_id": "c-1",
        "stored_outcome": "success",
        "stored_tenant": _TENANT,
    }
    row.update(overrides)
    return row


# ── FEEDBACK-IDEMP-01: the replay key ─────────────────────────────────────


class TestOutcomeReplayKey:
    async def test_caller_outcome_id_is_kept_and_written_with_merge_after_the_constraint(self) -> None:
        from engine.handlers import handle_outcomes

        driver = _Driver()
        _wire(driver, _spec())
        result = await handle_outcomes(_TENANT, _payload(outcome_id="replay-1"))

        assert result == {"status": "recorded", "outcome_id": "replay-1"}
        constraint, write = driver.cyphers()[:2]
        assert "CREATE CONSTRAINT ceg_outcome_id_" in constraint
        assert "REQUIRE o.outcome_id IS UNIQUE" in constraint
        assert "MERGE (o:" in write
        assert "CREATE (o:" not in write

    async def test_without_outcome_id_the_server_mints_one_and_creates(self) -> None:
        from engine.handlers import handle_outcomes

        driver = _Driver()
        _wire(driver, _spec())
        result = await handle_outcomes(_TENANT, _payload())

        assert result["outcome_id"].startswith("out_")
        assert not any("CREATE CONSTRAINT" in c for c in driver.cyphers())
        assert "CREATE (o:" in driver.cyphers()[0]

    @pytest.mark.parametrize("bad", ["", " lead", "a" * 129, "has space", "semi;colon", 42, None])
    async def test_malformed_outcome_id_is_refused_before_any_write(self, bad: Any) -> None:
        from engine.handlers import ValidationError, handle_outcomes

        driver = _Driver()
        _wire(driver, _spec())
        payload = _payload()
        payload["outcome_id"] = bad
        if bad is None:
            # An explicit null is not a key; the server mints one.
            assert (await handle_outcomes(_TENANT, payload))["outcome_id"].startswith("out_")
            return
        with pytest.raises(ValidationError, match="outcome_id must be"):
            await handle_outcomes(_TENANT, payload)
        assert driver.calls == []

    async def test_constraint_failure_fails_closed_with_nothing_written(self) -> None:
        from engine.handlers import ExecutionError, handle_outcomes

        driver = _Driver(constraint_error=RuntimeError("no schema rights"))
        _wire(driver, _spec())
        with pytest.raises(ExecutionError, match="could not enforce outcome_id uniqueness"):
            await handle_outcomes(_TENANT, _payload(outcome_id="replay-1"))
        assert len(driver.calls) == 1

    async def test_replay_reports_replayed_and_skips_first_write_side_effects(self, monkeypatch) -> None:
        from engine import handlers

        compliance = MagicMock(enabled=True)
        monkeypatch.setattr(handlers, "_get_compliance_engine", lambda _spec: compliance)
        driver = _Driver(write_rows=[_stored()])
        _wire(driver, _spec())
        result = await handlers.handle_outcomes(_TENANT, _payload(outcome_id="replay-1"))

        assert result == {"status": "recorded", "outcome_id": "replay-1", "replayed": True}
        compliance.log_outcome.assert_not_called()

    @pytest.mark.parametrize(
        ("field", "value"),
        [("match_id", "m-other"), ("candidate_id", "c-other"), ("outcome", "failure")],
    )
    async def test_replay_with_different_content_is_refused(self, field: str, value: str) -> None:
        from engine.handlers import ValidationError, handle_outcomes

        driver = _Driver(write_rows=[_stored()])
        _wire(driver, _spec())
        with pytest.raises(ValidationError, match=f"already recorded with different {field}"):
            await handle_outcomes(_TENANT, _payload(outcome_id="replay-1", **{field: value}))

    async def test_flag_off_ignores_the_caller_id(self, monkeypatch) -> None:
        from engine.handlers import handle_outcomes

        monkeypatch.setattr(settings, "outcome_idempotency_enabled", False)
        driver = _Driver()
        _wire(driver, _spec())
        result = await handle_outcomes(_TENANT, _payload(outcome_id="replay-1"))

        assert result["outcome_id"].startswith("out_")
        assert all("MERGE (o:" not in c for c in driver.cyphers())

    async def test_gated_off_feedback_never_runs_the_loop(self, monkeypatch) -> None:
        from engine.feedback import convergence
        from engine.handlers import handle_outcomes

        loop_cls = MagicMock()
        monkeypatch.setattr(convergence, "ConvergenceLoop", loop_cls)
        monkeypatch.setattr(settings, "feedback_enabled", False)
        _wire(_Driver(), _spec(feedback_enabled=True))
        result = await handle_outcomes(_TENANT, _payload(outcome_id="replay-1"))

        assert "feedback" not in result
        loop_cls.assert_not_called()

    async def test_replay_still_runs_the_loop_so_a_failed_propagation_can_finish(self, monkeypatch) -> None:
        from engine.feedback import convergence
        from engine.handlers import handle_outcomes

        loop = MagicMock()
        loop.on_outcome_recorded = AsyncMock(return_value={"feedback_loop": "enabled", "status": "complete"})
        monkeypatch.setattr(convergence, "ConvergenceLoop", MagicMock(return_value=loop))
        monkeypatch.setattr(settings, "feedback_enabled", True)
        _wire(_Driver(write_rows=[_stored()]), _spec(feedback_enabled=True))
        result = await handle_outcomes(_TENANT, _payload(outcome_id="replay-1"))

        assert result["replayed"] is True
        assert result["feedback"]["status"] == "complete"
        assert loop.on_outcome_recorded.await_args.args[0]["outcome_id"] == "replay-1"


# ── FEEDBACK-IDEMP-01: the propagation claim ──────────────────────────────


class TestPropagationClaim:
    @staticmethod
    def _propagator(rows: list[dict[str, Any]]) -> tuple[Any, MagicMock]:
        from engine.feedback.score_propagator import ScorePropagator

        driver = MagicMock()
        driver.execute_query = AsyncMock(return_value=rows)
        return ScorePropagator(driver, _spec(feedback_enabled=True)), driver

    async def test_first_application_reports_affected(self) -> None:
        propagator, driver = self._propagator([{"applied": True, "affected": 2}])
        result = await propagator.propagate_outcome(
            {"outcome_id": "o-1", "outcome": "success", "candidate_id": "c-1"}, boost_factor=1.15
        )
        assert result["propagated"] is True
        assert result["candidates_affected"] == 2
        cypher = driver.execute_query.await_args.args[0]
        assert "SET claim._propagation_lock = true" in cypher
        assert "SET dw._propagation_lock = true" in cypher
        assert driver.execute_query.await_args.kwargs["parameters"]["outcome_id"] == "o-1"

    async def test_already_applied_outcome_does_not_propagate(self) -> None:
        propagator, _ = self._propagator([{"applied": False, "affected": 0}])
        result = await propagator.propagate_outcome(
            {"outcome_id": "o-1", "outcome": "failure", "candidate_id": "c-1"}, boost_factor=1.15
        )
        assert result == {"propagated": False, "reason": "already_applied", "outcome_id": "o-1"}

    async def test_unknown_outcome_id_does_not_propagate(self) -> None:
        propagator, _ = self._propagator([])
        result = await propagator.propagate_outcome(
            {"outcome_id": "o-missing", "outcome": "success", "candidate_id": "c-1"}, boost_factor=1.15
        )
        assert result["reason"] == "outcome_not_found"

    async def test_without_outcome_id_or_flag_the_legacy_query_runs(self, monkeypatch) -> None:
        propagator, driver = self._propagator([{"affected": 3}])
        await propagator.propagate_outcome({"outcome": "success", "candidate_id": "c-1"}, boost_factor=1.15)
        assert "claim" not in driver.execute_query.await_args.args[0]

        monkeypatch.setattr(settings, "outcome_idempotency_enabled", False)
        await propagator.propagate_outcome(
            {"outcome_id": "o-1", "outcome": "success", "candidate_id": "c-1"}, boost_factor=1.15
        )
        assert "claim" not in driver.execute_query.await_args.args[0]


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
        with pytest.raises(RuntimeError):
            await self._loop(propagation=RuntimeError("boom")).on_outcome_recorded(self._OUTCOME)


# ── Sync-path exclusion (documented decision, FEATURE_GATES §30) ──────────


async def test_sync_outcomes_never_run_the_convergence_loop(monkeypatch) -> None:
    from engine.feedback import convergence
    from engine.handlers import handle_sync

    loop_cls = MagicMock()
    monkeypatch.setattr(convergence, "ConvergenceLoop", loop_cls)
    monkeypatch.setattr(settings, "feedback_enabled", True)
    _wire(_Driver(), _spec(feedback_enabled=True))
    result = await handle_sync(
        _TENANT,
        {"entity_type": "outcome", "batch": [{"match_id": "m-1", "chosen_candidate_id": "c-1", "outcome": "success"}]},
    )
    assert result["entity_type"] == "outcome"
    loop_cls.assert_not_called()
