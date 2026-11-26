"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, feedback]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_feedback_per_class_minimum.py
SCR-13 (feedback part) on a real graph: outcomes recorded by ``handle_outcomes``
are read back by admin ``score_feedback``, which proposes a weight nudge only
once each outcome class holds ``feedback_min_samples_per_class`` scored
outcomes. Every graph write goes through the outcomes handler.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import handle_admin, handle_outcomes, init_dependencies
from engine.state import get_state
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

TENANT = "it-feedback-minimum"


@pytest_asyncio.fixture
async def graph(
    graph_driver: GraphDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[GraphDriver, None]:
    spec = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())
    spec["domain"]["id"] = TENANT
    spec["feedbackloop"]["enabled"] = True
    (tmp_path / TENANT).mkdir()
    (tmp_path / TENANT / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    monkeypatch.setattr(settings, "feedback_enabled", True)
    monkeypatch.setattr(settings, "outcome_schema_unified", True)
    monkeypatch.setattr(settings, "feedback_per_class_minimum_enabled", True)
    monkeypatch.setattr(settings, "feedback_min_samples_per_class", 5)
    await graph_driver.execute_query(f"CREATE DATABASE `{TENANT}` IF NOT EXISTS WAIT", database="system")
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))
    yield graph_driver
    get_state().reset()
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)


async def _record(outcome: str, count: int, reliability: float) -> None:
    for i in range(count):
        result = await handle_outcomes(
            TENANT,
            {
                "match_id": f"{outcome}-{i}",
                "candidate_id": f"depot-{outcome}-{i}",
                "outcome": outcome,
                "fingerprint": {"dimension_scores": {"depot_reliability": reliability}},
            },
        )
        assert result["status"] == "recorded"


async def _feedback() -> dict[str, Any]:
    result = await handle_admin(TENANT, {"subaction": "score_feedback", "domain_id": TENANT})
    assert result["status"] == "feedback_computed"
    return result


async def test_score_feedback_needs_the_per_class_minimum_of_recorded_outcomes(graph: GraphDriver) -> None:
    # Ten recorded outcomes, both classes present, but only four failures.
    await _record("success", 5, 0.9)
    await _record("failure", 4, 0.2)
    await _record("partial", 1, 0.5)
    short = await _feedback()
    assert short["sample_count"] == 10
    assert short["sufficient_data"] is False
    assert short["proposed_weights"] == {}
    analysis = short["dimension_analysis"]["depot_reliability"]
    assert (analysis["positive_count"], analysis["negative_count"]) == (5, 4)

    # The fifth failure reaches the minimum; the proposal is the usual capped nudge.
    await _record("failure", 1, 0.2)
    enough = await _feedback()
    assert enough["sufficient_data"] is True
    assert enough["proposed_weights"] == {"depot_reliability": pytest.approx(0.014)}


async def test_flag_off_one_recorded_failure_was_enough(graph: GraphDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    await _record("success", 9, 0.9)
    await _record("failure", 1, 0.2)
    monkeypatch.setattr(settings, "feedback_per_class_minimum_enabled", False)
    legacy = await _feedback()
    assert legacy["sufficient_data"] is True  # the defect the flag closes
