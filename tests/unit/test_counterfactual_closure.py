"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [causal, counterfactual, unit]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 N6 (F08; cases C01, C05, C06, C16 source unit).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.causal.counterfactual import CounterfactualGenerator
from engine.config.schema import CounterfactualSpec
from engine.config.settings import settings

pytestmark = [pytest.mark.unit, pytest.mark.finding("F08")]

ACTUAL = {"active_dimensions": ["alpha"], "dimension_weights": {"alpha": 0.1}, "gates_passed": []}
POSITIVES = [
    {"active_dimensions": ["alpha", "gamma"], "dimension_weights": {"alpha": 0.5}, "outcome_id": "p1"},
    {"active_dimensions": ["alpha", "gamma"], "dimension_weights": {"alpha": 0.6}, "outcome_id": "p2"},
    {"active_dimensions": ["alpha", "delta"], "dimension_weights": {"alpha": 0.1}, "outcome_id": "p3"},
]


def _generator(**spec: object) -> CounterfactualGenerator:
    cf = CounterfactualSpec(enabled=True, **{"max_scenarios_per_outcome": 3, "min_confidence": 0.3, **spec})
    return CounterfactualGenerator(cf, AsyncMock(), "d", "WidgetOutcome")


def _semantics(scenarios: list[dict]) -> list[tuple]:
    return sorted((s["intervention_type"], s["key_difference"], s["counterfactual_outcome"]) for s in scenarios)


def test_default_legacy_output_asserts_untested_success() -> None:
    """C01 / F08 witness (BLOCKED_OWNER_DECISION): the shipped default claims success.

    ``counterfactual_support_semantics_enabled`` defaults off, so the default
    generator labels a never-run intervention ``counterfactual_outcome:
    success`` with a co-occurrence share named ``confidence``. That output is
    not a verified prediction; flipping the default is an operator /
    domain-owner policy decision, so this test pins the current default.
    """
    assert settings.counterfactual_support_semantics_enabled is False
    out = _generator()._diff_fingerprints(ACTUAL, POSITIVES, "o1")
    assert out
    assert {s["counterfactual_outcome"] for s in out} == {"success"}
    assert all("confidence" in s and "claim" not in s for s in out)


@pytest.mark.parametrize("support_mode", [False, True])
def test_scenarios_are_deduped_bounded_and_deterministic(monkeypatch, support_mode: bool) -> None:
    """C05 / C06: one scenario per intervention key, at most the cap, same semantics on rerun."""
    monkeypatch.setattr(settings, "counterfactual_support_semantics_enabled", support_mode)
    gen = _generator()
    diff = gen._diff_fingerprints_support if support_mode else gen._diff_fingerprints
    first = diff(actual_fingerprint=ACTUAL, positive_fingerprints=POSITIVES, outcome_node_id="o1")
    second = diff(actual_fingerprint=ACTUAL, positive_fingerprints=POSITIVES, outcome_node_id="o1")

    keys = [(s["intervention_type"], s["key_difference"]) for s in first]
    assert len(keys) == len(set(keys))
    assert ("change_weight", "alpha_weight_increase") in keys
    assert ("add_scoring_dimension", "delta") in keys  # support 1/3 >= minimum 0.3
    assert len(first) <= 3
    assert _semantics(first) == _semantics(second)

    capped = _generator(max_scenarios_per_outcome=1)
    capped_diff = capped._diff_fingerprints_support if support_mode else capped._diff_fingerprints
    assert len(capped_diff(actual_fingerprint=ACTUAL, positive_fingerprints=POSITIVES, outcome_node_id="o1")) == 1


@pytest.mark.asyncio
async def test_store_merges_on_semantic_key_and_returns_stored_ids() -> None:
    """C16: the write is a MERGE on the intervention key; a stored id replaces the proposed one."""
    gen = _generator()
    gen._graph_driver.execute_query = AsyncMock(
        return_value=[
            {"proposed_id": "cf_new", "stored": {"scenario_id": "cf_old", "confidence": 0.5, "domain_id": "d"}}
        ]
    )
    scenarios = [
        {
            "scenario_id": "cf_new",
            "actual_outcome": "o1",
            "counterfactual_outcome": "success",
            "intervention_type": "add_scoring_dimension",
            "confidence": 0.6,
            "key_difference": "gamma",
        }
    ]
    await gen._store_scenarios(scenarios)
    cypher = gen._graph_driver.execute_query.call_args.kwargs["cypher"]
    assert "MERGE (cs:CounterfactualScenario {actual_outcome: s.actual_outcome" in cypher
    assert "CREATE (cs:CounterfactualScenario" not in cypher
    assert scenarios[0]["scenario_id"] == "cf_old"
    assert scenarios[0]["confidence"] == 0.5  # stored evidence, not the recomputed 0.6
    assert "domain_id" not in scenarios[0]


@pytest.mark.asyncio
async def test_dedupe_flag_off_restores_create(monkeypatch) -> None:
    """COUNTERFACTUAL_DEDUPE_AND_SPEC_LABEL=false keeps the legacy CREATE (rollback)."""
    monkeypatch.setattr(settings, "counterfactual_dedupe_and_spec_label", False)
    gen = _generator()
    gen._graph_driver.execute_query = AsyncMock(return_value=[])
    await gen._store_scenarios([])
    assert "CREATE (cs:CounterfactualScenario" in gen._graph_driver.execute_query.call_args.kwargs["cypher"]
