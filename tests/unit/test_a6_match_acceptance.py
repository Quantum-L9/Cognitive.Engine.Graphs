"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, match, gates, scoring, acceptance]
owner: engine-team
status: active
--- /L9_META ---

A6 (EIE-CEG-GATE-COLLAB-2026-11-21) — matching on the synced graph, pinned at
the Cypher `handle_match` actually sends for the PlasticOS design pack (a
witness, not a dependency) and the neutral synthetic tenant.

P33: polymer capability is a hard, directed gate in WHERE, before scoring.
P34: wrong polymer and missing active supply fail; unknown contamination
     tolerance stays eligible where the spec says ``nullbehavior: pass``.
P35: one row per candidate node before ORDER BY/LIMIT, bounded top_n, absent
     dimension sources reported rather than silent.
P05: the same assertions hold for a tenant with no polymer vocabulary.

Execution against Neo4j is integration (testcontainers) and is not run here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.loader import DomainPackLoader
from engine.handlers import handle_match, init_dependencies
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, synthetic_loader

pytestmark = pytest.mark.unit

FORWARD = "supply_opportunity_to_buyer_facility"
REVERSE = "buyer_demand_to_supply_opportunity"


class _CapturingDriver:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((cypher, dict(parameters or {})))
        return list(self.rows)


@pytest.fixture(autouse=True)
def _reset():
    from engine.state import _reset_singleton

    yield
    _reset_singleton()


def _bind(domain: str, loader: DomainPackLoader, rows: list[dict[str, Any]] | None = None) -> _CapturingDriver:
    driver = _CapturingDriver(rows)
    stub = MagicMock()
    stub.load_domain.return_value = loader.load_domain(domain)
    stub.list_domains.return_value = [domain]
    init_dependencies(driver, stub)  # type: ignore[arg-type]
    return driver


async def _plasticos_match(direction: str, query: dict[str, Any], **extra: Any) -> tuple[str, dict[str, Any], Any]:
    driver = _bind("plasticos", DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT)), extra.pop("rows", None))
    result = await handle_match("plasticos", {"match_direction": direction, "query": query, **extra})
    cypher, params = driver.calls[-1]
    return cypher, params, result


def _where(cypher: str) -> str:
    start = cypher.index("\nWHERE ")
    return cypher[start : cypher.index("\nWITH candidate,", start)]


# ── P33 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polymer_capability_is_a_directed_hard_gate_before_scoring() -> None:
    cypher, params, _ = await _plasticos_match(FORWARD, {"polymer_type": "HDPE"})
    where = _where(cypher)
    assert "EXISTS { MATCH (candidate)-[rel:PROCESSES]->(t:PolymerFamily) WHERE t.code = $polymer_type" in where
    assert "rel.direction = $gate_polymer_capability_gate_edge0" in where
    assert params["gate_polymer_capability_gate_edge0"] == "in"
    assert "rel.form IS NOT NULL" in where
    # Hard eligibility precedes soft ranking: the WHERE sits before the first ORDER BY.
    assert cypher.index("\nWHERE ") < cypher.index("ORDER BY")
    assert params["polymer_type"] == "HDPE"


@pytest.mark.asyncio
async def test_reverse_direction_requires_fulfills_polymer_not_facility_capability() -> None:
    cypher, _, _ = await _plasticos_match(REVERSE, {"polymer_type": "HDPE", "volume_tons": 10})
    where = _where(cypher)
    assert "MATCH (candidate)-[:FULFILLS_POLYMER]->(t:PolymerFamily) WHERE t.code = $polymer_type" in where
    assert "PROCESSES" not in where


# ── P34 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_polymer_and_missing_active_supply_fail() -> None:
    cypher, params, _ = await _plasticos_match(REVERSE, {"polymer_type": "HDPE", "volume_tons": 10})
    where = _where(cypher)
    # nullbehavior: fail — an absent value cannot satisfy the gate.
    assert (
        "candidate.polymer_type IS NOT NULL AND $polymer_type IS NOT NULL AND (candidate.polymer_type = $polymer_type)"
        in where
    )
    assert "candidate.active IS NOT NULL AND $requires_active_supply IS NOT NULL" in where
    assert params["requires_active_supply"] is True
    # A polymer gate with no requested polymer admits nothing.
    assert "$polymer_type IS NOT NULL AND (EXISTS" in where


@pytest.mark.asyncio
async def test_unknown_tolerance_stays_eligible_known_tolerance_still_compares() -> None:
    cypher, _, _ = await _plasticos_match(FORWARD, {"polymer_type": "HDPE", "contamination_pct": 2.0})
    where = _where(cypher)
    assert (
        "(candidate.contamination_tolerance IS NULL OR $contamination_pct IS NULL OR "
        "(candidate.contamination_tolerance >= $contamination_pct))"
    ) in where


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [FORWARD, REVERSE])
async def test_no_universal_grade_or_colour_exclusion(direction: str) -> None:
    cypher, _, _ = await _plasticos_match(direction, {"polymer_type": "HDPE", "volume_tons": 10})
    where = _where(cypher).lower()
    assert "color" not in where
    assert "colour" not in where
    assert "grade" not in where.replace("food_grade", "")


# ── P35 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_row_per_candidate_node_before_limit() -> None:
    cypher, _, _ = await _plasticos_match(FORWARD, {"polymer_type": "HDPE"})
    collapse = cypher.index("WITH candidate, head(collect(")
    assert collapse < cypher.rindex("ORDER BY score DESC") < cypher.index("LIMIT $top_n")


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "bound"), [(5, 5), (100_000, 1000)])
async def test_top_n_is_bounded(requested: int, bound: int) -> None:
    _, params, _ = await _plasticos_match(FORWARD, {"polymer_type": "HDPE"}, top_n=requested)
    assert params["top_n"] == bound


@pytest.mark.asyncio
async def test_absent_dimension_sources_are_reported_per_candidate() -> None:
    rows = [
        {
            "candidate": {"facility_id": "f1"},
            "score": 0.4,
            "dimension_scores": {"geo_proximity": 0.0},
            "absent_sources": ["geo_proximity"],
        }
    ]
    cypher, _, result = await _plasticos_match(FORWARD, {"polymer_type": "HDPE"}, rows=rows)
    assert "AS absent_sources" in cypher
    [candidate] = result["candidates"]
    assert "geo_proximity" in candidate["missing_evidence"]


# ── P05 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neutral_tenant_match_carries_no_polymer_vocabulary() -> None:
    loader = synthetic_loader()
    spec = loader.load_domain("orbital-logistics")
    direction = spec.queryschema.matchdirections[0]
    driver = _bind("orbital-logistics", loader)
    await handle_match("orbital-logistics", {"match_direction": direction, "query": {}})
    cypher = driver.calls[-1][0]
    for literal in ("polymer", "PROCESSES", "PolymerFamily", "contamination", "Facility"):
        assert literal not in cypher
    assert "LIMIT $top_n" in cypher
