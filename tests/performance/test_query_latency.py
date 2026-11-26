"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, performance, latency]
owner: engine-team
status: active
--- /L9_META ---

tests/performance/test_query_latency.py
Match latency, measured on the engine's own path (Contract 17, TEST_PATTERNS:
p95 match latency < 200 ms).

The timed call is ``handle_match`` itself: request validation, parameter
resolution, gate/traversal/scoring compilation, the Cypher the engine emits and
its result shaping. The graph is written by ``handle_sync`` (no hand-seeded
nodes, no hand-assembled query). Two shapes on the synthetic orbital-logistics
pack, each at CANDIDATES synced depots:

* the pack as declared (boolean + threshold gates, two scoring dimensions);
* the pack plus its SERVES_ORBIT traversal gate (tests/fixtures/orbital_match),
  so the strict chain carries an EXISTS subquery over synced taxonomy edges.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any

import pytest
import structlog

from tests.fixtures.orbital_match import DIRECTION, ORBITAL, orbit_gated, serve

pytestmark = pytest.mark.performance

logger = structlog.get_logger(__name__)

CANDIDATES = 200  # synced depots, every one of which passes the gates
TOP_N = 25
WARMUP = 5
ITERATIONS = 50
P95_BOUND_MS = 200.0  # docs/contracts/TEST_PATTERNS.md


def _depots(served_orbits: list[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for i in range(CANDIDATES):
        row: dict[str, Any] = {
            "depot_id": f"bench-{i:04d}",
            "accepts_hazmat": True,
            "docking_ports": 2 + i % 6,
            "reliability": round(0.5 + (i % 50) / 100, 2),
        }
        if served_orbits is not None:
            row["served_orbits"] = served_orbits
        rows.append(row)
    return rows


async def _sync(entity_type: str, batch: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(ORBITAL, {"entity_type": entity_type, "batch": batch})
    assert result["status"] == "success"


async def _p95_ms(payload: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Warm up, then time ITERATIONS calls of handle_match; returns (p95 ms, last result)."""
    from engine.handlers import handle_match

    result: dict[str, Any] = {}
    for _ in range(WARMUP):
        result = await handle_match(ORBITAL, payload)
    latencies: list[float] = []
    for _ in range(ITERATIONS):
        start = time.perf_counter()
        result = await handle_match(ORBITAL, payload)
        latencies.append((time.perf_counter() - start) * 1000)
    ordered = sorted(latencies)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]  # nearest-rank p95
    logger.info(
        "handle_match_latency",
        candidates=CANDIDATES,
        top_n=TOP_N,
        n=ITERATIONS,
        p50_ms=round(statistics.median(latencies), 1),
        p95_ms=round(p95, 1),
        max_ms=round(ordered[-1], 1),
    )
    return p95, result


@pytest.mark.asyncio
async def test_handle_match_p95_under_200ms_on_the_declared_pack(engine_deps, clean_db):
    await _sync("orbital-depots", _depots())

    p95, result = await _p95_ms({"query": {"min_ports": 2}, "match_direction": DIRECTION, "top_n": TOP_N})

    assert len(result["candidates"]) == TOP_N  # the timed query did the full work, not an empty match
    assert p95 < P95_BOUND_MS, f"handle_match p95 {p95:.1f} ms at {CANDIDATES} candidates exceeds {P95_BOUND_MS} ms"


@pytest.mark.asyncio
async def test_handle_match_p95_under_200ms_with_a_traversal_gate(engine_deps, graph_driver, domain_spec, clean_db):
    serve(graph_driver, orbit_gated(domain_spec))
    await _sync("orbit-classes", [{"class_code": "GEO"}, {"class_code": "LEO"}])
    await _sync("orbital-depots", _depots(served_orbits=["GEO"]))

    payload = {"query": {"min_ports": 2, "orbit_class": "GEO"}, "match_direction": DIRECTION, "top_n": TOP_N}
    p95, result = await _p95_ms(payload)

    assert len(result["candidates"]) == TOP_N
    assert p95 < P95_BOUND_MS, f"handle_match p95 {p95:.1f} ms at {CANDIDATES} candidates exceeds {P95_BOUND_MS} ms"

    from engine.handlers import handle_match

    # The traversal gate is live: no depot serves LEO, so nothing passes.
    leo = await handle_match(ORBITAL, {**payload, "query": {"min_ports": 2, "orbit_class": "LEO"}})
    assert leo["candidates"] == []
