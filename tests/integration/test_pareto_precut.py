"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, handlers, match, pareto, arbitration]
owner: engine-team
status: active
--- /L9_META ---

SI-06: with ``pareto_precut_enabled`` the Pareto front decides which candidates
fill ``top_n``, against a real Neo4j on the synthetic orbital-logistics pack.

Scores are the pack's own: ``0.6 * reliability + 0.3 * log(1 + ports) / log(1001)``.
``dominated`` outscores ``wide`` but is beaten by ``best`` on both objectives;
``wide`` has the lowest score and the most docking ports, so it is non-dominated.
"""

from __future__ import annotations

import pytest

from engine.config.schema import DecisionArbitrationSpec, ParetoConfigSpec, ParetoObjectiveSpec
from engine.config.settings import settings
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, depot, serve

pytestmark = pytest.mark.integration

SEED_CYPHER = """
UNWIND $batch AS row
MERGE (n:OrbitalDepot {depot_id: row.depot_id})
SET n += row
"""

DEPOTS = [
    depot("best", reliability=0.95, docking_ports=20),
    depot("dominated", reliability=0.90, docking_ports=10),
    depot("wide", reliability=0.30, docking_ports=900),
]


def _arbitrated(spec):
    objectives = [ParetoObjectiveSpec(dimension="depot_reliability"), ParetoObjectiveSpec(dimension="port_capacity")]
    arbitration = DecisionArbitrationSpec(enabled=True, pareto_config=ParetoConfigSpec(objectives=objectives))
    return spec.model_copy(update={"decision_arbitration": arbitration})


async def _top_two(graph_driver, spec) -> list[str]:
    from engine.handlers import handle_match

    serve(graph_driver, spec)
    result = await handle_match(ORBITAL, {"query": {}, "match_direction": DIRECTION, "top_n": 2})
    return [c["candidate"]["depot_id"] for c in result["candidates"]]


@pytest.mark.asyncio
async def test_precut_replaces_dominated_candidate_in_top_n(
    engine_deps, graph_driver, domain_spec, clean_db, monkeypatch
):
    await graph_driver.execute_write(cypher=SEED_CYPHER, parameters={"batch": DEPOTS}, database=ORBITAL)
    spec = _arbitrated(domain_spec)

    monkeypatch.setattr(settings, "pareto_precut_enabled", False)
    assert await _top_two(graph_driver, spec) == ["best", "dominated"]

    monkeypatch.setattr(settings, "pareto_precut_enabled", True)
    assert await _top_two(graph_driver, spec) == ["best", "wide"]
