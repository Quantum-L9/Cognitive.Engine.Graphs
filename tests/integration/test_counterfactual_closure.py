"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [causal, counterfactual, integration]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 N6 (F08, F10; cases C03, C04, C13, C15, C16, C17).

``CounterfactualGenerator`` against a real Neo4j: graph read-back of the
persisted scenarios, a legitimate empty comparison pool, retry idempotency,
and the post-outcome failure path of ``handle_outcomes``. Every test isolates
its rows by a unique ``match_direction`` / ``run_tag`` so parallel workers on
one server never see each other's outcomes.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from engine.causal.counterfactual import CounterfactualGenerator
from engine.config.schema import CounterfactualSpec, DomainSpec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.finding("F08")]

_DB = "neo4j"
_LABEL = "WidgetOutcome"


class _Seed:
    def __init__(self, driver: Any, tag: str) -> None:
        self.driver = driver
        self.tag = tag
        self.direction = f"dir_{tag}"

    async def outcome(self, outcome_id: str, outcome: str, dims: list[str], weights: dict[str, float]) -> None:
        await self.driver.execute_write(
            cypher=(
                f"CREATE (o:{_LABEL} {{outcome_id: $oid, outcome: $outcome, active_dimensions: $dims, "
                "dimension_weights: $weights, gates_passed: [], match_direction: $dir, "
                "candidate_count: 3, created_at: datetime(), run_tag: $tag})"
            ),
            parameters={
                "oid": outcome_id,
                "outcome": outcome,
                "dims": dims,
                "weights": json.dumps(weights),
                "dir": self.direction,
                "tag": self.tag,
            },
            database=_DB,
        )

    async def scenarios(self, outcome_id: str) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(
            cypher=(
                "MATCH (cs:CounterfactualScenario {actual_outcome: $oid}) "
                "RETURN properties(cs) AS p ORDER BY cs.intervention_type, cs.key_difference"
            ),
            parameters={"oid": outcome_id},
            database=_DB,
        )
        return [r["p"] for r in rows]


@pytest_asyncio.fixture
async def seed(graph_driver) -> AsyncGenerator[_Seed, None]:
    tag = f"cf_closure_{uuid.uuid4().hex[:12]}"
    s = _Seed(graph_driver, tag)
    yield s
    await graph_driver.execute_write(
        cypher=(
            "MATCH (n) WHERE n.run_tag = $tag OR (n:CounterfactualScenario AND n.actual_outcome STARTS WITH $tag) "
            "OR (n:WidgetOutcome AND n.match_id STARTS WITH $tag) DETACH DELETE n"
        ),
        parameters={"tag": tag},
        database=_DB,
    )


def _generator(driver: Any, **spec: Any) -> CounterfactualGenerator:
    cf = CounterfactualSpec(enabled=True, **{"max_scenarios_per_outcome": 3, "min_confidence": 0.3, **spec})
    return CounterfactualGenerator(counterfactual_spec=cf, graph_driver=driver, domain_id=_DB, outcome_label=_LABEL)


async def _seed_contrast(seed: _Seed) -> str:
    negative = f"{seed.tag}_neg"
    await seed.outcome(negative, "failure", ["price"], {"price": 0.2})
    await seed.outcome(f"{seed.tag}_p1", "success", ["price", "geo"], {"price": 0.5, "geo": 0.5})
    await seed.outcome(f"{seed.tag}_p2", "success", ["price", "geo"], {"price": 0.6, "geo": 0.4})
    await seed.outcome(f"{seed.tag}_p3", "success", ["price"], {"price": 0.1})
    return negative


@pytest.mark.asyncio
async def test_support_mode_persists_observational_fields(seed, graph_driver, monkeypatch) -> None:
    """C03: read-back carries support/basis/pool and no invented confidence or success."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", True)
    negative = await _seed_contrast(seed)

    returned = await _generator(graph_driver).generate_for_outcome(negative)
    stored = await seed.scenarios(negative)

    assert len(stored) == len(returned) == 2
    by_key = {(s["intervention_type"], s["key_difference"]): s for s in stored}
    geo = by_key[("add_scoring_dimension", "geo")]
    assert geo["support_count"] == 2
    assert geo["compared_positives"] == 3
    assert geo["support"] == pytest.approx(0.6667)
    weight = by_key[("change_weight", "price_weight_increase")]
    assert weight["support_count"] == 2
    for s in stored:
        assert s["counterfactual_outcome"] == "untested"
        assert s["claim"] == "co_occurrence"
        assert s["support_basis"] == "share_of_compared_positive_outcomes"
        assert "confidence" not in s
        assert s["domain_id"] == _DB
    assert {s["scenario_id"] for s in stored} == {s["scenario_id"] for s in returned}


@pytest.mark.asyncio
async def test_no_positive_pool_yields_zero_scenarios(seed, graph_driver) -> None:
    """C04: a negative outcome with no positive comparison pool writes nothing."""
    negative = f"{seed.tag}_lonely"
    await seed.outcome(negative, "failure", ["price"], {"price": 0.2})

    assert await _generator(graph_driver).generate_for_outcome(negative) == []
    assert await seed.scenarios(negative) == []


@pytest.mark.parametrize("support_mode", [False, True])
@pytest.mark.asyncio
async def test_retry_does_not_duplicate_scenarios(seed, graph_driver, monkeypatch, support_mode: bool) -> None:
    """C16: a second run for a stored outcome adds no duplicate semantic scenario."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", support_mode)
    negative = await _seed_contrast(seed)
    generator = _generator(graph_driver)

    first = await generator.generate_for_outcome(negative)
    after_first = await seed.scenarios(negative)
    second = await generator.generate_for_outcome(negative)
    after_second = await seed.scenarios(negative)

    assert len(after_first) == len(first) == 2
    assert len(after_second) == 2, "a retry created duplicate CounterfactualScenario nodes"
    assert {s["scenario_id"] for s in after_second} == {s["scenario_id"] for s in after_first}
    assert {s["scenario_id"] for s in second} == {s["scenario_id"] for s in first}


@pytest.mark.asyncio
async def test_retry_over_changed_pool_returns_stored_evidence(seed, graph_driver, monkeypatch) -> None:
    """C16: first write wins; a retry after the pool grew returns what the graph holds."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", True)
    negative = await _seed_contrast(seed)
    generator = _generator(graph_driver)
    await generator.generate_for_outcome(negative)
    await seed.outcome(f"{seed.tag}_p4", "success", ["price", "geo"], {"price": 0.7, "geo": 0.3})

    retried = await generator.generate_for_outcome(negative)
    stored = {s["scenario_id"]: s for s in await seed.scenarios(negative)}

    assert len(stored) == 2
    for scenario in retried:
        node = stored[scenario["scenario_id"]]
        for key in ("support", "support_count", "compared_positives", "claim", "counterfactual_outcome"):
            assert scenario[key] == node[key]
    assert {s["compared_positives"] for s in retried} == {3}


@pytest.mark.asyncio
async def test_missing_fingerprint_and_persistence_error_are_not_success(seed, graph_driver) -> None:
    """C17: no fingerprint -> no scenario; a failed scenario write raises, never returns as stored."""
    assert await _generator(graph_driver).generate_for_outcome(f"{seed.tag}_absent") == []

    negative = await _seed_contrast(seed)

    class _FailingWrites:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def execute_query(self, cypher: str, parameters: Any = None, database: Any = None) -> Any:
            if "CounterfactualScenario" in cypher:
                msg = "scenario write refused"
                raise RuntimeError(msg)
            return await self._inner.execute_query(cypher=cypher, parameters=parameters, database=database)

    with pytest.raises(RuntimeError, match="scenario write refused"):
        await _generator(_FailingWrites(graph_driver)).generate_for_outcome(negative)
    assert await seed.scenarios(negative) == []


def _outcome_spec(base: DomainSpec) -> DomainSpec:
    raw = base.model_dump(by_alias=True)
    raw["domain"]["id"] = _DB
    raw["counterfactual"] = {"enabled": True, "max_scenarios_per_outcome": 3, "min_confidence": 0.3}
    raw["feedbackloop"] = {**raw.get("feedbackloop", {}), "outcome_node_label": _LABEL}
    return DomainSpec.model_validate(raw)


class _Loader:
    def __init__(self, spec: DomainSpec) -> None:
        self._spec = spec

    def load_domain(self, _tenant: str) -> DomainSpec:
        return self._spec


async def _bind(graph_driver: Any, spec: DomainSpec) -> None:
    from engine.handlers import init_dependencies
    from engine.state import get_state

    get_state().reset()
    init_dependencies(graph_driver, _Loader(spec))


def _failure_payload(seed: _Seed, outcome_id: str) -> dict[str, Any]:
    return {
        "match_id": f"{seed.tag}_match",
        "candidate_id": f"{seed.tag}_cand",
        "outcome": "failure",
        "outcome_id": outcome_id,
        "fingerprint": {
            "active_dimensions": ["price"],
            "dimension_weights": {"price": 0.2},
            "gates_passed": [],
            "match_direction": seed.direction,
            "candidate_count": 3,
        },
    }


async def _outcome_nodes(graph_driver: Any, outcome_id: str) -> int:
    rows = await graph_driver.execute_query(
        cypher=f"MATCH (o:{_LABEL} {{outcome_id: $oid}}) RETURN count(o) AS n",
        parameters={"oid": outcome_id},
        database=_DB,
    )
    return int(rows[0]["n"])


@pytest.mark.asyncio
async def test_repeat_outcome_records_one_observation(seed, graph_driver, minimal_domain_spec) -> None:
    """C15: the same outcome_id twice -> one observation node and no duplicate scenarios."""
    from engine.handlers import handle_outcomes
    from engine.state import get_state

    await _seed_contrast(seed)
    await _bind(graph_driver, _outcome_spec(minimal_domain_spec))
    outcome_id = f"{seed.tag}_handled"
    try:
        first = await handle_outcomes(_DB, _failure_payload(seed, outcome_id))
        scenarios_first = await seed.scenarios(outcome_id)
        second = await handle_outcomes(_DB, _failure_payload(seed, outcome_id))
        scenarios_second = await seed.scenarios(outcome_id)
    finally:
        get_state().reset()

    assert first["status"] == second["status"] == "recorded"
    assert second.get("replayed") is True
    assert await _outcome_nodes(graph_driver, outcome_id) == 1
    assert len(scenarios_first) == 2
    assert {s["scenario_id"] for s in scenarios_second} == {s["scenario_id"] for s in scenarios_first}


@pytest.mark.asyncio
async def test_counterfactual_stage_failure_keeps_outcome(
    seed, graph_driver, minimal_domain_spec, monkeypatch, caplog
) -> None:
    """C13 / F10 witness: the outcome survives a generator failure; a replay does not rerun the stage.

    The stage failure is visible only as a WARNING log ("Counterfactual
    generation failed"); the response carries no counterfactual status and a
    replay of the same outcome_id skips the stage (handle_outcomes, owned by
    the parallel campaign: N7-HANDOFF), so no scenario is ever produced.
    """
    from engine.handlers import handle_outcomes
    from engine.state import get_state

    await _seed_contrast(seed)
    await _bind(graph_driver, _outcome_spec(minimal_domain_spec))
    outcome_id = f"{seed.tag}_flaky"

    async def _boom(self: Any, _oid: str) -> list[dict[str, Any]]:
        msg = "transient counterfactual failure"
        raise RuntimeError(msg)

    try:
        with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="engine.handlers"):
            patch.setattr(CounterfactualGenerator, "generate_for_outcome", _boom)
            first = await handle_outcomes(_DB, _failure_payload(seed, outcome_id))
        replay = await handle_outcomes(_DB, _failure_payload(seed, outcome_id))
    finally:
        get_state().reset()

    assert await _outcome_nodes(graph_driver, outcome_id) == 1
    assert first["status"] == "recorded"
    assert "counterfactual" not in first  # no completion claim is made
    # C13 evidence: the stage failure is surfaced in the handler's WARNING log,
    # naming the outcome and carrying the exception (no response field: the
    # public outcome payload shape is frozen for this campaign).
    failures = [
        r
        for r in caplog.records
        if r.name == "engine.handlers" and "Counterfactual generation failed" in r.getMessage()
    ]
    assert len(failures) == 1
    assert outcome_id in failures[0].getMessage()
    assert failures[0].levelno == logging.WARNING
    assert failures[0].exc_info is not None
    assert "transient counterfactual failure" in str(failures[0].exc_info[1])
    assert replay.get("replayed") is True
    # F10 (CONDITIONAL C14): the replay path does not recover the failed stage.
    assert await seed.scenarios(outcome_id) == []
