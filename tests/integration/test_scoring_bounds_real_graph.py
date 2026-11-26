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

Scoring bounds and effective null strategies on a real Neo4j (CEG scoring audit L2, D3a / D5).

Candidates are written by the engine's own write path (``handle_sync``) and
scored by ``handle_match``; the pack is the synthetic ``orbital-logistics``
pack with a declared ``_prior_depot_reliability`` property and a
``_popmean_depot_reliability`` query field, written to a temporary pack root.

DimensionWeight rows are the one piece of state seeded with Cypher: no engine
write path produces a non-finite or negative learned weight (the calculator
clamps to [min_weight, max_weight]); the rows model corrupt or externally
written state the reader must survive, which is what D5 requires.
"""

from __future__ import annotations

import copy
import math
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


def _pack(mutate: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    raw = copy.deepcopy(yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8")))
    depot = next(node for node in raw["ontology"]["nodes"] if node["label"] == "OrbitalDepot")
    depot["properties"].append({"name": "_prior_depot_reliability", "type": "float"})
    raw["queryschema"]["fields"].append({"name": "_popmean_depot_reliability", "type": "float", "required": False})
    reliability = raw["scoring"]["dimensions"][0]
    assert reliability["name"] == "depot_reliability"
    reliability["defaultwhennull"] = 0.1
    if mutate is not None:
        mutate(raw)
    return raw


def _strategy(strategy: str) -> Callable[[dict[str, Any]], None]:
    def mutate(raw: dict[str, Any]) -> None:
        raw["scoring"]["dimensions"][0]["null_strategy"] = strategy

    return mutate


def _feedback(raw: dict[str, Any]) -> None:
    raw["feedbackloop"] = {**raw["feedbackloop"], "enabled": True, "signal_weights": {"enabled": True}}


@pytest_asyncio.fixture
async def serve(tmp_path: Path, graph_driver, domain_database) -> AsyncGenerator[Callable[..., None], None]:
    """Serve a variant of orbital-logistics from a temporary pack root; wipe its database afterwards."""
    from engine.config.loader import DomainPackLoader
    from engine.handlers import init_dependencies
    from engine.state import get_state

    def _serve(mutate: Callable[[dict[str, Any]], None] | None = None) -> None:
        pack_dir = tmp_path / _DOMAIN
        pack_dir.mkdir(exist_ok=True)
        (pack_dir / "spec.yaml").write_text(yaml.safe_dump(_pack(mutate), sort_keys=False), encoding="utf-8")
        get_state().reset()
        init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))

    yield _serve
    get_state().reset()
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=domain_database)


async def _sync(depots: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(_DOMAIN, {"entity_type": "orbital-depots", "batch": depots})
    assert result.get("status") in ("ok", "success"), result


async def _match(query: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    from engine.handlers import handle_match

    result = await handle_match(_DOMAIN, {"query": query or {}, "match_direction": _DIRECTION, "top_n": 20})
    return {row["candidate"]["depot_id"]: row for row in result["candidates"]}


def _depot(depot_id: str, **props: Any) -> dict[str, Any]:
    return {"depot_id": depot_id, "accepts_hazmat": True, "docking_ports": 10, **props}


# ── D3a: effective null strategies ───────────────────────────────────────────


async def test_inherit_prior_takes_the_prior_for_a_missing_property(serve) -> None:
    serve(_strategy("inherit_prior"))
    await _sync(
        [
            _depot("present", reliability=0.9, _prior_depot_reliability=0.4),
            _depot("missing-with-prior", _prior_depot_reliability=0.4),
            _depot("missing-no-prior"),
        ]
    )
    rows = await _match()
    assert rows["present"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.9)
    assert rows["missing-with-prior"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.4)
    assert rows["missing-no-prior"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.1)


async def test_population_mean_takes_the_bound_mean_for_a_missing_property(serve) -> None:
    serve(_strategy("population_mean"))
    await _sync([_depot("present", reliability=0.9), _depot("missing")])
    rows = await _match({"_popmean_depot_reliability": 0.55})
    assert rows["present"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.9)
    assert rows["missing"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.55)


async def test_population_mean_unbound_falls_back_to_defaultwhennull(serve) -> None:
    serve(_strategy("population_mean"))
    await _sync([_depot("missing")])
    rows = await _match()
    assert rows["missing"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.1)


async def test_flag_off_the_legacy_wrapper_ignores_the_prior(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect D3a fixes, on a real graph: the inner coalesce answers first."""
    monkeypatch.setattr(settings, "null_strategy_effective_enabled", False)
    serve(_strategy("inherit_prior"))
    await _sync([_depot("missing-with-prior", _prior_depot_reliability=0.4)])
    rows = await _match()
    assert rows["missing-with-prior"]["dimension_scores"]["depot_reliability"] == pytest.approx(0.1)


# ── D5: the composite is bounded to [0, 1] ───────────────────────────────────


async def _seed_learned_weight(graph_driver, dimension: str, weight: float) -> None:
    await graph_driver.execute_write(
        cypher="MERGE (dw:DimensionWeight {tenant: $tenant, dimension: $dimension}) SET dw.weight = $weight",
        parameters={"tenant": _DOMAIN, "dimension": dimension, "weight": weight},
        database=_DOMAIN,
    )


_PORT_HEAVY = _depot("port-heavy", reliability=0.0, docking_ports=900)
_BALANCED = _depot("balanced", reliability=0.7, docking_ports=50)
_BASELINE = {
    # 0.6 * reliability + 0.3 * log(1 + ports) / log(1001)
    "port-heavy": 0.3 * math.log(901) / math.log(1001),
    "balanced": 0.6 * 0.7 + 0.3 * math.log(51) / math.log(1001),
}


async def test_no_learning_scores_are_the_declared_weighted_sum(serve) -> None:
    serve(_feedback)
    await _sync([_PORT_HEAVY, _BALANCED])
    rows = await _match()
    for depot_id, expected in _BASELINE.items():
        assert rows[depot_id]["score"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("learned", [0.0, -2.0], ids=["zero", "negative"])
async def test_penalised_learned_weight_subtracts_and_floors_at_zero(serve, graph_driver, learned: float) -> None:
    serve(_feedback)
    await _sync([_PORT_HEAVY, _BALANCED])
    await _seed_learned_weight(graph_driver, "port_capacity", learned)
    rows = await _match()
    # Penalised: port_capacity's coefficient is -penalty_factor (-0.3).
    assert rows["port-heavy"]["score"] == 0.0  # 0.0 - 0.3 * 0.985 clamps to 0.0
    balanced = 0.6 * 0.7 - 0.3 * math.log(51) / math.log(1001)
    assert rows["balanced"]["score"] == pytest.approx(balanced, abs=1e-9)
    for row in rows.values():
        assert 0.0 <= row["score"] <= 1.0


@pytest.mark.parametrize("learned", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "neg-inf"])
async def test_non_finite_learned_weight_is_ignored(serve, graph_driver, learned: float) -> None:
    serve(_feedback)
    await _sync([_PORT_HEAVY, _BALANCED])
    await _seed_learned_weight(graph_driver, "depot_reliability", learned)
    rows = await _match()
    for depot_id, expected in _BASELINE.items():
        assert rows[depot_id]["score"] == pytest.approx(expected, abs=1e-9)
        assert 0.0 <= rows[depot_id]["score"] <= 1.0


async def test_normal_positive_learned_weight_stays_in_range(serve, graph_driver) -> None:
    serve(_feedback)
    await _sync([_PORT_HEAVY, _BALANCED])
    await _seed_learned_weight(graph_driver, "depot_reliability", 2.5)
    rows = await _match()
    for row in rows.values():
        assert 0.0 <= row["score"] <= 1.0
    # Learning moved weight towards reliability: the reliable depot gains, the port-heavy one loses.
    assert rows["balanced"]["score"] > _BASELINE["balanced"]
    assert rows["port-heavy"]["score"] < _BASELINE["port-heavy"]
