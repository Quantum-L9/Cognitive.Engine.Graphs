"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, neo4j]
owner: engine-team
status: active
--- /L9_META ---

Per-dimension knownness on a real Neo4j (CEG-13, ``dimension_knownness_projection_enabled``).

The pack is the synthetic ``orbital-logistics`` pack with an optional traversal
step ``(candidate:OrbitalDepot)-[:STOCKS]->(grade:PropellantGrade)`` and a
``traversalalias`` dimension ``grade_purity`` reading ``grade.purity``, written
to a temporary pack root. Depots and their stocked grades are written by the
engine's own write path (``handle_sync``, childsync) and scored by
``handle_match``. The alias is in scope only in the first scoring WITH, which is
where the knownness is computed.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from engine.config.settings import settings
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DOMAIN = "orbital-logistics"
_DIRECTION = "window_to_depot"
_PURITY = {
    "name": "grade_purity",
    "source": "traversalalias",
    "computation": "traversalalias",
    "alias": "grade",
    "candidateprop": "purity",
    "weightkey": "w_purity",
    "defaultweight": 0.1,
    "defaultwhennull": 0.0,
}


def _pack() -> dict[str, Any]:
    raw = copy.deepcopy(yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8")))
    raw["traversal"] = {
        **(raw.get("traversal") or {}),
        "steps": [
            {
                "name": "stocked_grade",
                "pattern": "(candidate:OrbitalDepot)-[:STOCKS]->(grade:PropellantGrade)",
                "required": False,
            }
        ],
    }
    raw["scoring"]["dimensions"].append(copy.deepcopy(_PURITY))
    return raw


@pytest_asyncio.fixture
async def serve(tmp_path: Path, graph_driver, domain_database) -> AsyncGenerator[Callable[[], None], None]:
    """Serve the variant pack from a temporary root; wipe its database afterwards."""
    from engine.config.loader import DomainPackLoader
    from engine.handlers import init_dependencies
    from engine.state import get_state

    def _serve() -> None:
        pack_dir = tmp_path / _DOMAIN
        pack_dir.mkdir(exist_ok=True)
        (pack_dir / "spec.yaml").write_text(yaml.safe_dump(_pack(), sort_keys=False), encoding="utf-8")
        get_state().reset()
        init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))

    yield _serve
    get_state().reset()
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=domain_database)


async def _sync(depots: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(_DOMAIN, {"entity_type": "orbital-depots", "batch": depots})
    assert result.get("status") in ("ok", "success"), result


async def _match() -> dict[str, dict[str, Any]]:
    from engine.handlers import handle_match

    result = await handle_match(_DOMAIN, {"query": {}, "match_direction": _DIRECTION, "top_n": 20})
    return {row["candidate"]["depot_id"]: row for row in result["candidates"]}


def _depot(depot_id: str, **props: Any) -> dict[str, Any]:
    return {"depot_id": depot_id, "accepts_hazmat": True, "docking_ports": 10, "reliability": 0.8, **props}


async def test_alias_sourced_dimension_knownness_follows_the_traversal_alias(serve) -> None:
    serve()
    await _sync(
        [
            _depot("stocked", stocked_grades=[{"grade_code": "LOX-A", "purity": 0.9}]),
            _depot("unrated", stocked_grades=[{"grade_code": "LOX-B"}]),
            _depot("bare"),
        ]
    )
    rows = await _match()
    assert rows["stocked"]["dimension_known"]["grade_purity"] is True
    assert rows["unrated"]["dimension_known"]["grade_purity"] is False
    assert rows["bare"]["dimension_known"]["grade_purity"] is False
    # Candidate-property dimensions are known for every depot (reliability, docking_ports written).
    for row in rows.values():
        assert row["dimension_known"]["depot_reliability"] is True
        assert row["dimension_known"]["port_capacity"] is True


async def test_knownness_leaves_scores_unchanged(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    serve()
    await _sync([_depot("stocked", stocked_grades=[{"grade_code": "LOX-A", "purity": 0.9}]), _depot("bare")])
    on = await _match()
    monkeypatch.setattr(settings, "dimension_knownness_projection_enabled", False)
    serve()
    off = await _match()
    assert {k: v["score"] for k, v in on.items()} == {k: v["score"] for k, v in off.items()}
    assert {k: v["dimension_scores"] for k, v in on.items()} == {k: v["dimension_scores"] for k, v in off.items()}
    assert all("dimension_known" not in row or row["dimension_known"] is None for row in off.values())
