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

CEG-462: feedback weight governance and the retired boost_factor stage.

- ``feedback_boost_propagation_enabled`` (default off): the convergence loop
  writes no ``boost_factor`` (a hand-set multiplier nothing reads) and says so.
- ``learned_weight_change_record_enabled`` (default on): every recalculation
  appends one ``DimensionWeightChange`` per dimension; nothing updates or
  deletes one.
- ``learned_weight_auto_apply_enabled`` (default off): a recalculation is an
  unapplied candidate and ``DimensionWeight.weight`` (what scoring reads) is
  not written; on, each applied write is recorded in the same statement.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from engine.config.settings import settings
from engine.feedback.convergence import ConvergenceLoop
from engine.feedback.signal_weights import SignalWeightCalculator, learned_weights_applied
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
_OUTCOME = {"outcome_id": "o-1", "outcome": "success", "candidate_id": "c-1"}


def _spec() -> Any:
    spec = synthetic_loader().load_domain("orbital-logistics")
    fl = spec.feedbackloop
    return spec.model_copy(
        update={
            "feedbackloop": fl.model_copy(
                update={"enabled": True, "signal_weights": fl.signal_weights.model_copy(update={"enabled": True})}
            )
        }
    )


class _Driver:
    """Records every statement and answers the feedback reads by shape."""

    def __init__(self, *, new_outcomes: int = 100, total: int = 20, wins: int = 10) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._new_outcomes = new_outcomes
        self._total = total
        self._wins = wins
        self.execute_query = AsyncMock(side_effect=self._execute)

    async def _execute(self, cypher: str, parameters: dict[str, Any] | None = None, **_: Any) -> list[dict[str, Any]]:
        parameters = parameters or {}
        self.calls.append((cypher, parameters))
        if "AS new_outcomes" in cypher:
            return [{"new_outcomes": self._new_outcomes}]
        if "AS dim_total" in cypher:
            return [{"dim_total": 10, "dim_wins": 8}]
        if "AS total" in cypher:
            return [{"total": self._total, "wins": self._wins}]
        if "DimensionWeightChange {" in cypher or "MERGE (dw:DimensionWeight" in cypher:
            return [{"dimension": parameters.get("dimension")}]
        return []

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(c, p) for c, p in self.calls if re.search(r"\b(SET|CREATE|MERGE)\b", c)]


def _sets_weight(cypher: str) -> bool:
    return re.search(r"SET\s+dw\.weight\b", cypher) is not None


# ── boost_factor retirement ────────────────────────────────────────────────


class TestBoostPropagationRetired:
    def test_default_is_off(self) -> None:
        assert settings.feedback_boost_propagation_enabled is False

    @pytest.mark.parametrize("isolated", [True, False])
    async def test_loop_writes_no_boost_factor_and_reports_not_applied(self, monkeypatch, isolated: bool) -> None:
        monkeypatch.setattr(settings, "feedback_partial_results_enabled", isolated)
        driver = _Driver(new_outcomes=0)
        loop = ConvergenceLoop(driver, _spec())  # type: ignore[arg-type]
        loop._score_propagator.propagate_outcome = AsyncMock()  # type: ignore[method-assign]

        metadata = await loop.on_outcome_recorded(_OUTCOME)

        loop._score_propagator.propagate_outcome.assert_not_awaited()
        assert metadata["propagation"] == {"propagated": False, "reason": "boost_propagation_disabled"}
        assert all("boost_factor" not in c for c, _ in driver.calls)
        if isolated:
            assert "propagation" not in metadata["stages"]
            assert metadata["status"] == "complete"

    async def test_flag_on_restores_the_legacy_stage(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "feedback_boost_propagation_enabled", True)
        loop = ConvergenceLoop(_Driver(new_outcomes=0), _spec())  # type: ignore[arg-type]
        loop._score_propagator.propagate_outcome = AsyncMock(return_value={"propagated": True})  # type: ignore[method-assign]

        metadata = await loop.on_outcome_recorded(_OUTCOME)

        loop._score_propagator.propagate_outcome.assert_awaited_once()
        assert metadata["stages"]["propagation"] == "ok"

    def test_no_engine_code_reads_boost_factor_for_ranking(self) -> None:
        """Retiring the stage cannot change ranking: only its writer names the property."""
        readers = sorted(
            str(p.relative_to(_ENGINE))
            for p in _ENGINE.rglob("*.py")
            if "boost_factor" in p.read_text(encoding="utf-8")
        )
        assert readers == [
            "config/schema.py",
            "config/settings.py",
            "feedback/convergence.py",
            "feedback/score_propagator.py",
        ]
        assert "boost_factor" not in (_ENGINE / "scoring" / "assembler.py").read_text(encoding="utf-8")


# ── weight governance ─────────────────────────────────────────────────────


class TestUnappliedCandidateByDefault:
    def test_defaults(self) -> None:
        assert settings.learned_weight_change_record_enabled is True
        assert settings.learned_weight_auto_apply_enabled is False
        assert learned_weights_applied() is False

    async def test_recalculation_records_candidates_and_never_writes_the_weight(self) -> None:
        driver = _Driver()
        metadata = await ConvergenceLoop(driver, _spec()).on_outcome_recorded(_OUTCOME)  # type: ignore[arg-type]

        assert metadata["weights_recalculated"] is True
        assert metadata["weights_applied"] is False
        writes = driver.writes()
        assert len(writes) == 2  # one record per scoring dimension
        for cypher, params in writes:
            assert not _sets_weight(cypher)
            assert "MERGE" not in cypher, "a candidate must not create a DimensionWeight node"
            assert "CREATE (c:DimensionWeightChange" in cypher
            assert "applied: false" in cypher
            assert "head(collect(dw.weight)) AS prior_weight" in cypher
            assert params["outcome_count"] == 20
            assert params["win_count"] == 10
            assert params["sample_size"] == 10
            assert params["method"] == "outcome_lift/agresti_coull/v1"
        assert {p["dimension"] for _, p in writes} == {"depot_reliability", "port_capacity"}
        assert len({p["recalculation_id"] for _, p in writes}) == 1

    async def test_each_recalculation_gets_its_own_id(self) -> None:
        driver = _Driver()
        calc = SignalWeightCalculator(driver, _spec())  # type: ignore[arg-type]
        await calc.recalculate_weights()
        await calc.recalculate_weights()
        ids = [p["recalculation_id"] for _, p in driver.writes()]
        assert len(ids) == 4
        assert len(set(ids)) == 2

    async def test_no_outcomes_writes_nothing(self) -> None:
        driver = _Driver(total=0, wins=0)
        weights = await SignalWeightCalculator(driver, _spec()).recalculate_weights()  # type: ignore[arg-type]
        assert weights == {"depot_reliability": 1.0, "port_capacity": 1.0}
        assert driver.writes() == []

    async def test_recalculation_marker_includes_unapplied_records(self) -> None:
        """Without it every outcome past the threshold would recalculate and record again."""
        driver = _Driver()
        await SignalWeightCalculator(driver, _spec()).should_recalculate()  # type: ignore[arg-type]
        cypher = driver.calls[0][0]
        assert "max(c.recorded_at) AS last_recorded" in cypher
        assert "max(dw.updated_at) AS last_applied" in cypher

    async def test_replayed_outcome_after_recalculation_does_not_recalculate(self) -> None:
        loop = ConvergenceLoop(_Driver(new_outcomes=0), _spec())  # type: ignore[arg-type]
        metadata = await loop.on_outcome_recorded(_OUTCOME)
        assert metadata["weights_recalculated"] is False
        assert metadata["weights_applied"] is False


class TestApprovedAutomaticRecalculation:
    async def test_each_applied_write_is_recorded_in_the_same_statement(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "learned_weight_auto_apply_enabled", True)
        assert learned_weights_applied() is True
        driver = _Driver()
        metadata = await ConvergenceLoop(driver, _spec()).on_outcome_recorded(_OUTCOME)  # type: ignore[arg-type]

        assert metadata["weights_applied"] is True
        writes = driver.writes()
        assert len(writes) == 2
        for cypher, _ in writes:
            assert _sets_weight(cypher)
            assert "CREATE (:DimensionWeightChange" in cypher
            assert "applied: true" in cypher
            # prior weight read under the write lock, before the new weight is set
            lock, prior, write = (
                cypher.index("SET dw._weight_lock"),
                cypher.index("dw.weight AS prior_weight"),
                cypher.index("SET dw.weight"),
            )
            assert lock < prior < write


class TestLegacyOverwrite:
    async def test_record_flag_off_overwrites_with_no_history(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "learned_weight_change_record_enabled", False)
        assert learned_weights_applied() is True
        driver = _Driver()
        calc = SignalWeightCalculator(driver, _spec())  # type: ignore[arg-type]
        await calc.recalculate_weights()
        writes = driver.writes()
        assert len(writes) == 2
        assert all(_sets_weight(c) and "DimensionWeightChange" not in c for c, _ in writes)

        driver.calls.clear()
        await calc.should_recalculate()
        assert "DimensionWeightChange" not in driver.calls[0][0]


def test_change_records_are_append_only() -> None:
    """L9 audit law: DimensionWeightChange is created, never updated or deleted, anywhere in the engine."""
    files = {p.relative_to(_ENGINE).as_posix(): p for p in _ENGINE.rglob("*.py")}
    writers = [n for n, p in files.items() if "CREATE (c:DimensionWeightChange" in p.read_text(encoding="utf-8")]
    assert writers == ["feedback/signal_weights.py"]
    for name, path in files.items():
        source = path.read_text(encoding="utf-8")
        if "DimensionWeightChange" not in source:
            continue
        assert not re.search(r"MERGE\s*\(\w*:DimensionWeightChange", source), name
        assert not re.search(
            r"\(\s*(\w+)\s*:DimensionWeightChange\b[^)]*\)[\s\S]*?\b(SET|REMOVE|DELETE)\s+\1\b", source
        ), name
        assert not re.search(r"DETACH\s+DELETE", source), name
    signal_weights = files["feedback/signal_weights.py"].read_text(encoding="utf-8")
    assert not re.search(r"\b(SET|REMOVE|DELETE)\s+c\b", signal_weights)
