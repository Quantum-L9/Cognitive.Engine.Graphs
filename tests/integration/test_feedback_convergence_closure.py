"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, feedback]
owner: engine-team
status: active
--- /L9_META ---

Integration — feedback convergence closure against Neo4j (FEEDBACK-IDEMP-01, FEEDBACK-PARTIAL-01).

Proves docs/contracts/feedback/convergence-loop.yaml required_proof and
blocking_validation through the real ``handle_outcomes`` path on the per-domain
``plasticos`` database (enterprise testcontainer):

- gated off: no loop, no weight change
- gated on: one outcome multiplies each DimensionWeight once
- replay: the same outcome_id twice applies once and records one node
- concurrent replay: two simultaneous first writes still apply once
- failure: a failed propagation reports partial, stamps nothing, and a retry
  with the same outcome_id applies it exactly once
- cross-tenant: another tenant's DimensionWeights are never touched, and a
  tenant outside the allowlist is refused before any write
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.config.settings import settings

pytestmark = pytest.mark.integration

_TENANT = "plasticos"
_FOREIGN_TENANT = "fbk-foreign-tenant"
_CANDIDATE = "FBK-CLOSURE-C1"
_DIMS = ("fbk_dim_a", "fbk_dim_b")


async def _seed(graph_driver: Any) -> None:
    await graph_driver.execute_query(
        """
        UNWIND $dims AS dim
        CREATE (:DimensionWeight {dimension: dim, tenant: $tenant, weight: 1.0, boost_factor: 1.0})
        WITH DISTINCT 1 AS one
        CREATE (:DimensionWeight {dimension: 'fbk_foreign', tenant: $foreign, weight: 1.0, boost_factor: 1.0})
        """,
        parameters={"dims": list(_DIMS), "tenant": _TENANT, "foreign": _FOREIGN_TENANT},
        database=_TENANT,
    )


async def _boosts(graph_driver: Any, tenant: str = _TENANT) -> list[float]:
    rows = await graph_driver.execute_query(
        "MATCH (dw:DimensionWeight {tenant: $tenant}) WHERE dw.dimension STARTS WITH 'fbk_' "
        "RETURN dw.boost_factor AS b ORDER BY dw.dimension",
        parameters={"tenant": tenant},
        database=_TENANT,
    )
    return [float(r["b"]) for r in rows]


async def _outcome_nodes(graph_driver: Any, outcome_id: str) -> list[dict[str, Any]]:
    label = "TransactionOutcome"
    rows: list[dict[str, Any]] = await graph_driver.execute_query(
        f"MATCH (o:{label} {{outcome_id: $outcome_id}}) "
        "RETURN o.propagated_at IS NOT NULL AS stamped, o.tenant AS tenant",
        parameters={"outcome_id": outcome_id},
        database=_TENANT,
    )
    return rows


@pytest.fixture
async def feedback_graph(engine_deps, graph_driver, domain_spec, clean_db, monkeypatch):
    assert domain_spec.feedbackloop.outcome_node_label == "TransactionOutcome"
    assert domain_spec.feedbackloop.enabled is True
    monkeypatch.setattr(settings, "feedback_enabled", True)
    monkeypatch.setattr(settings, "outcome_idempotency_enabled", True)
    monkeypatch.setattr(settings, "feedback_partial_results_enabled", True)
    monkeypatch.setattr(settings, "propagation_single_application", True)
    await _seed(graph_driver)
    return graph_driver


def _payload(outcome_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"match_id": "fbk-match-1", "candidate_id": _CANDIDATE, "outcome": "success"}
    if outcome_id is not None:
        payload["outcome_id"] = outcome_id
    payload.update(overrides)
    return payload


async def test_gated_off_writes_the_outcome_and_moves_no_weight(feedback_graph, monkeypatch):
    from engine.handlers import handle_outcomes

    monkeypatch.setattr(settings, "feedback_enabled", False)
    result = await handle_outcomes(_TENANT, _payload("fbk-off-1"))

    assert result == {"status": "recorded", "outcome_id": "fbk-off-1"}
    assert len(await _outcome_nodes(feedback_graph, "fbk-off-1")) == 1
    assert await _boosts(feedback_graph) == [pytest.approx(1.0), pytest.approx(1.0)]


async def test_gated_on_applies_the_boost_once(feedback_graph, domain_spec):
    from engine.handlers import handle_outcomes

    factor = domain_spec.feedbackloop.propagation_boost_factor
    result = await handle_outcomes(_TENANT, _payload("fbk-on-1"))

    assert result["feedback"]["status"] == "complete"
    assert result["feedback"]["propagation"]["propagated"] is True
    assert result["feedback"]["propagation"]["candidates_affected"] == len(_DIMS)
    assert await _boosts(feedback_graph) == [pytest.approx(factor)] * len(_DIMS)
    assert (await _outcome_nodes(feedback_graph, "fbk-on-1"))[0]["stamped"] is True


async def test_replayed_outcome_applies_once_and_records_one_node(feedback_graph, domain_spec):
    from engine.handlers import handle_outcomes

    factor = domain_spec.feedbackloop.propagation_boost_factor
    first = await handle_outcomes(_TENANT, _payload("fbk-replay-1"))
    after_first = await _boosts(feedback_graph)
    second = await handle_outcomes(_TENANT, _payload("fbk-replay-1"))

    assert "replayed" not in first
    assert second["replayed"] is True
    assert second["feedback"]["propagation"] == {
        "propagated": False,
        "reason": "already_applied",
        "outcome_id": "fbk-replay-1",
    }
    assert after_first == [pytest.approx(factor)] * len(_DIMS)
    assert await _boosts(feedback_graph) == after_first
    assert len(await _outcome_nodes(feedback_graph, "fbk-replay-1")) == 1


async def test_concurrent_first_writes_of_one_outcome_apply_once(feedback_graph, domain_spec):
    from engine.handlers import ExecutionError, handle_outcomes

    factor = domain_spec.feedbackloop.propagation_boost_factor
    results = await asyncio.gather(
        *(handle_outcomes(_TENANT, _payload("fbk-race-1")) for _ in range(4)),
        return_exceptions=True,
    )
    # A writer that loses the uniqueness race is refused by the database
    # (ExecutionError); none may apply the boost a second time.
    unexpected = [r for r in results if isinstance(r, BaseException) and not isinstance(r, ExecutionError)]
    assert not unexpected, unexpected
    applied = [
        r for r in results if isinstance(r, dict) and r.get("feedback", {}).get("propagation", {}).get("propagated")
    ]
    assert len(applied) == 1
    assert len(await _outcome_nodes(feedback_graph, "fbk-race-1")) == 1
    assert await _boosts(feedback_graph) == [pytest.approx(factor)] * len(_DIMS)


async def test_failed_propagation_reports_partial_and_a_retry_applies_once(feedback_graph, domain_spec, monkeypatch):
    from engine.feedback.score_propagator import ScorePropagator
    from engine.handlers import handle_outcomes

    factor = domain_spec.feedbackloop.propagation_boost_factor
    real = ScorePropagator._apply_claimed_boost
    calls = {"n": 0}

    async def _fail_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "injected propagation failure"
            raise RuntimeError(msg)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(ScorePropagator, "_apply_claimed_boost", _fail_once)

    first = await handle_outcomes(_TENANT, _payload("fbk-retry-1"))
    assert first["status"] == "recorded"
    assert first["feedback"]["status"] == "partial"
    assert first["feedback"]["stages"]["propagation"] == "failed:RuntimeError"
    assert (await _outcome_nodes(feedback_graph, "fbk-retry-1"))[0]["stamped"] is False
    assert await _boosts(feedback_graph) == [pytest.approx(1.0)] * len(_DIMS)

    retry = await handle_outcomes(_TENANT, _payload("fbk-retry-1"))
    assert retry["replayed"] is True
    assert retry["feedback"]["status"] == "complete"
    assert retry["feedback"]["propagation"]["propagated"] is True
    assert await _boosts(feedback_graph) == [pytest.approx(factor)] * len(_DIMS)

    third = await handle_outcomes(_TENANT, _payload("fbk-retry-1"))
    assert third["feedback"]["propagation"]["reason"] == "already_applied"
    assert await _boosts(feedback_graph) == [pytest.approx(factor)] * len(_DIMS)


async def test_propagation_never_touches_another_tenants_weights(feedback_graph):
    from engine.handlers import handle_outcomes

    await handle_outcomes(_TENANT, _payload("fbk-tenant-1"))
    assert await _boosts(feedback_graph, _FOREIGN_TENANT) == [pytest.approx(1.0)]


async def test_tenant_outside_allowlist_is_refused_before_any_write(feedback_graph, monkeypatch):
    from engine.handlers import ValidationError, handle_outcomes
    from engine.state import get_state

    monkeypatch.setattr(get_state(), "_tenant_allowlist", {_TENANT})
    with pytest.raises(ValidationError):
        await handle_outcomes(_FOREIGN_TENANT, _payload("fbk-intruder-1"))
    assert await _outcome_nodes(feedback_graph, "fbk-intruder-1") == []
    assert await _boosts(feedback_graph) == [pytest.approx(1.0)] * len(_DIMS)
