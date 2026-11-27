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

Scoring compiler defaults on a real Neo4j (CEG scoring audit L7: SCR-01, SCR-07, SCR-08).

Candidates are written by the engine's own write path (``handle_sync``) and
scored by ``handle_match``. The pack is the synthetic ``orbital-logistics``
pack (``depot_reliability`` 0.6 + ``port_capacity`` 0.3) with an added
weightedrate dimension ``rate_quality`` (0.1) over ``reliability`` and the
default confidence property ``confidence``, written to a temporary pack root.

The DimensionWeight row is the one piece of state seeded with Cypher, as in
``test_scoring_bounds_real_graph.py``: it models the learned weight a
recalculation writes.
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
_RATE = {
    "name": "rate_quality",
    "source": "candidateproperty",
    "candidateprop": "reliability",
    "computation": "weightedrate",
    "weightkey": "w_rate",
    "defaultweight": 0.1,
    "defaultwhennull": 0.05,
}


def _pack(mutate: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    raw = copy.deepcopy(yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / _DOMAIN / "spec.yaml").read_text(encoding="utf-8")))
    depot = next(node for node in raw["ontology"]["nodes"] if node["label"] == "OrbitalDepot")
    depot["properties"] += [
        {"name": "confidence", "type": "float"},
        {"name": "value", "type": "float"},
        {"name": "_prior_rate_quality", "type": "float"},
    ]
    names = [dim["name"] for dim in raw["scoring"]["dimensions"]]
    assert names[:2] == ["depot_reliability", "port_capacity"], names
    raw["scoring"]["dimensions"].append(copy.deepcopy(_RATE))
    if mutate is not None:
        mutate(raw)
    return raw


def _dimension(raw: dict[str, Any], name: str) -> dict[str, Any]:
    return next(dim for dim in raw["scoring"]["dimensions"] if dim["name"] == name)


def _feedback(raw: dict[str, Any]) -> None:
    raw["feedbackloop"] = {**raw["feedbackloop"], "enabled": True, "signal_weights": {"enabled": True}}


@pytest_asyncio.fixture
async def serve(
    tmp_path: Path, graph_driver, domain_database, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Callable[..., None], None]:
    """Serve a variant of orbital-logistics from a temporary pack root; wipe its database afterwards.

    Learned weights load only when the global FEEDBACK_ENABLED and the pack's
    feedbackloop are both on (``_feedback_effective``); the global switch is on here.
    """
    from engine.config.loader import DomainPackLoader
    from engine.handlers import init_dependencies
    from engine.state import get_state

    monkeypatch.setattr(settings, "feedback_enabled", True)

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


async def _match(query: dict[str, Any] | None = None, weights: dict[str, float] | None = None) -> dict[str, Any]:
    from engine.handlers import handle_match

    payload: dict[str, Any] = {"query": query or {}, "match_direction": _DIRECTION, "top_n": 20}
    if weights is not None:
        payload["weights"] = weights
    result = await handle_match(_DOMAIN, payload)
    return {row["candidate"]["depot_id"]: row for row in result["candidates"]}


async def _seed_learned_weight(graph_driver, dimension: str, weight: float) -> None:
    await graph_driver.execute_write(
        cypher="MERGE (dw:DimensionWeight {tenant: $tenant, dimension: $dimension}) SET dw.weight = $weight",
        parameters={"tenant": _DOMAIN, "dimension": dimension, "weight": weight},
        database=_DOMAIN,
    )


def _depot(depot_id: str, **props: Any) -> dict[str, Any]:
    return {"depot_id": depot_id, "accepts_hazmat": True, "docking_ports": 10, **props}


def _ports(ports: int) -> float:
    return math.log(1 + ports) / math.log(1001)


# ── SCR-01: a 0 pre-learning weight excludes the dimension ──────────────────


async def test_zero_request_weight_excludes_the_dimension_from_the_match(serve) -> None:
    serve()
    await _sync([_depot("a", reliability=0.8, confidence=1.0, docking_ports=500)])
    row = (await _match(weights={"w_ports": 0.0}))["a"]
    assert "port_capacity" not in row["dimension_scores"]
    assert row["score"] == pytest.approx(0.6 * 0.8 + 0.1 * 0.8, abs=1e-9)


async def test_a_learned_penalty_does_not_reach_a_zero_weighted_dimension(serve, graph_driver) -> None:
    serve(_feedback)
    await _sync([_depot("a", reliability=0.8, confidence=1.0, docking_ports=500)])
    await _seed_learned_weight(graph_driver, "port_capacity", 0.0)  # below penalty_threshold 0.5
    row = (await _match(weights={"w_ports": 0.0}))["a"]
    assert "port_capacity" not in row["dimension_scores"]
    assert row["score"] == pytest.approx(0.6 * 0.8 + 0.1 * 0.8, abs=1e-9)


async def test_flag_off_a_zero_weighted_dimension_takes_the_penalty(
    serve, graph_driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect SCR-01 fixes: a weight the request set to 0 subtracts 0.3 * ports."""
    monkeypatch.setattr(settings, "zero_weight_dimension_excluded_enabled", False)
    serve(_feedback)
    await _sync([_depot("a", reliability=0.8, confidence=1.0, docking_ports=500)])
    await _seed_learned_weight(graph_driver, "port_capacity", 0.0)
    row = (await _match(weights={"w_ports": 0.0}))["a"]
    assert row["score"] == pytest.approx(0.6 * 0.8 + 0.1 * 0.8 - 0.3 * _ports(500), abs=1e-9)


# ── SCR-07: absent_sources follows the compiler's property resolution ───────


def _ports_reads_value(raw: dict[str, Any]) -> None:
    _dimension(raw, "port_capacity").pop("candidateprop")


async def test_absent_sources_reports_a_defaulted_candidateprop_and_the_confidence(serve) -> None:
    serve(_ports_reads_value)
    await _sync([_depot("complete", reliability=0.8, confidence=0.9, value=40.0), _depot("bare", reliability=0.8)])
    rows = await _match()
    assert sorted(rows["bare"]["absent_sources"]) == ["port_capacity", "rate_quality"]
    assert rows["complete"]["absent_sources"] == []
    assert {"port_capacity", "rate_quality"} <= set(rows["bare"]["missing_evidence"])
    assert not {"port_capacity", "rate_quality"} & set(rows["complete"]["missing_evidence"])


async def test_absent_sources_flag_off_misses_the_defaulted_property(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "absent_sources_compiler_resolution_enabled", False)
    serve(_ports_reads_value)
    await _sync([_depot("bare", reliability=0.8)])
    rows = await _match()
    assert rows["bare"]["absent_sources"] == []


# ── SCR-08: weightedrate with an unknown confidence ──────────────────────────


async def test_missing_confidence_scores_defaultwhennull_under_zero(serve) -> None:
    serve()
    await _sync([_depot("known", reliability=0.8, confidence=0.5), _depot("unknown", reliability=0.8)])
    rows = await _match()
    assert rows["known"]["dimension_scores"]["rate_quality"] == pytest.approx(0.4)
    assert rows["unknown"]["dimension_scores"]["rate_quality"] == pytest.approx(0.05)


async def test_missing_confidence_takes_the_prior(serve) -> None:
    def prior(raw: dict[str, Any]) -> None:
        _dimension(raw, "rate_quality")["null_strategy"] = "inherit_prior"

    serve(prior)
    await _sync(
        [
            _depot("known", reliability=0.8, confidence=0.5, _prior_rate_quality=0.3),
            _depot("unknown", reliability=0.8, _prior_rate_quality=0.3),
        ]
    )
    rows = await _match()
    assert rows["known"]["dimension_scores"]["rate_quality"] == pytest.approx(0.4)
    assert rows["unknown"]["dimension_scores"]["rate_quality"] == pytest.approx(0.3)


async def test_flag_off_missing_confidence_is_read_as_one(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "weightedrate_unknown_confidence_enabled", False)
    serve()
    await _sync([_depot("unknown", reliability=0.8)])
    rows = await _match()
    assert rows["unknown"]["dimension_scores"]["rate_quality"] == pytest.approx(0.8)
