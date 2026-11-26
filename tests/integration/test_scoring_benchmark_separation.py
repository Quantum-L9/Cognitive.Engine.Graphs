"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, benchmark, neo4j]
owner: engine-team
status: active
--- /L9_META ---

Score quality benchmark on a real graph (W5-03; CEG scoring audit L2, CTO answer).

The labelled ``GOOD_PAIRS`` / ``BAD_PAIRS`` (``tests/fixtures/benchmark_data.py``)
run through the engine's own path: candidates are written by ``handle_sync`` and
each pair's query is scored by ``handle_match`` on Neo4j. The documented gate —
good pairs outscore bad pairs by more than 0.20 on average — is asserted on the
engine's scores. This replaces the former test-local re-implementation of the
scoring math (``tests/scoring/test_benchmark.py``), which could not fail on an
engine change.

The pack is the benchmark spec ``tests/scoring/test_benchmark.py`` builds
(geodecay, candidate property, community match, log-normalised density),
declared here as YAML with a sync endpoint and written to a temporary pack root.
"""

from __future__ import annotations

import statistics
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from tests.fixtures.benchmark_data import BAD_PAIRS, GOOD_PAIRS, BenchmarkPair

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DOMAIN = "benchmark-scoring"
_DIRECTION = "query_to_facility"
_SEPARATION = 0.20


def _pack() -> dict[str, Any]:
    facility_props = [
        {"name": "facility_id", "type": "string", "required": True},
        {"name": "lat", "type": "float"},
        {"name": "lon", "type": "float"},
        {"name": "rate", "type": "float"},
        {"name": "community_id", "type": "int"},
        {"name": "density", "type": "float"},
        {"name": "active", "type": "bool"},
    ]
    return {
        "domain": {"id": _DOMAIN, "name": "Scoring benchmark (synthetic)", "version": "0.1.0"},
        "ontology": {
            "nodes": [
                {
                    "label": "Facility",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": _DIRECTION,
                    "properties": facility_props,
                },
                {
                    "label": "BenchmarkQuery",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": _DIRECTION,
                    "properties": [{"name": "query_id", "type": "string", "required": True}],
                },
            ],
            "edges": [],
        },
        "matchentities": {
            "candidate": [{"label": "Facility", "matchdirection": _DIRECTION}],
            "queryentity": [{"label": "BenchmarkQuery", "matchdirection": _DIRECTION}],
        },
        "queryschema": {
            "matchdirections": [_DIRECTION],
            "fields": [
                {"name": "lat", "type": "float", "required": False},
                {"name": "lon", "type": "float", "required": False},
                {"name": "community_id", "type": "int", "required": False},
            ],
        },
        "traversal": {"steps": []},
        "gates": [],
        "scoring": {
            "dimensions": [
                {
                    "name": "geo_proximity",
                    "source": "computed",
                    "computation": "geodecay",
                    "candidateprop": "lat",
                    "candidateprop_lon": "lon",
                    "queryprop": "lat",
                    "queryprop_lon": "lon",
                    "decayconstant": 50000.0,
                    "weightkey": "w_geo",
                    "defaultweight": 0.30,
                },
                {
                    "name": "structural",
                    "source": "candidateproperty",
                    "computation": "candidateproperty",
                    "candidateprop": "rate",
                    "weightkey": "w_structural",
                    "defaultweight": 0.30,
                },
                {
                    "name": "community",
                    "source": "computed",
                    "computation": "communitymatch",
                    "candidateprop": "community_id",
                    "queryprop": "community_id",
                    "weightkey": "w_community",
                    "defaultweight": 0.20,
                },
                {
                    "name": "density_score",
                    "source": "candidateproperty",
                    "computation": "lognormalized",
                    "candidateprop": "density",
                    "maxvalue": 100.0,
                    "weightkey": "w_density",
                    "defaultweight": 0.20,
                },
            ]
        },
        "sync": {
            "endpoints": [
                {
                    "path": "/v1/sync/facilities",
                    "method": "POST",
                    "targetnode": "Facility",
                    "idproperty": "facility_id",
                    "batchstrategy": "UNWINDMERGE",
                }
            ]
        },
    }


@pytest_asyncio.fixture
async def benchmark_pack(tmp_path: Path, graph_driver) -> AsyncGenerator[None, None]:
    """Serve the benchmark pack from a temporary root, in its own database; wipe it afterwards."""
    from engine.config.loader import DomainPackLoader
    from engine.handlers import init_dependencies
    from engine.state import get_state
    from tests.conftest import _create_database

    await _create_database(graph_driver, _DOMAIN)
    (tmp_path / _DOMAIN).mkdir()
    (tmp_path / _DOMAIN / "spec.yaml").write_text(yaml.safe_dump(_pack(), sort_keys=False), encoding="utf-8")
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))
    yield
    get_state().reset()
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=_DOMAIN)


def _facility(pair: BenchmarkPair) -> dict[str, Any]:
    return {"facility_id": pair.label, **pair.candidate_props}


async def _engine_score(pair: BenchmarkPair) -> float:
    """The engine's score for the pair's candidate under the pair's query."""
    from engine.handlers import handle_match

    top_n = len(GOOD_PAIRS) + len(BAD_PAIRS)
    result = await handle_match(
        _DOMAIN, {"query": dict(pair.query_params), "match_direction": _DIRECTION, "top_n": top_n}
    )
    scores = {row["candidate"]["facility_id"]: row["score"] for row in result["candidates"]}
    assert pair.label in scores, f"{pair.label} was not returned by handle_match"
    return float(scores[pair.label])


async def _sync_all() -> None:
    from engine.handlers import handle_sync

    batch = [_facility(pair) for pair in GOOD_PAIRS + BAD_PAIRS]
    result = await handle_sync(_DOMAIN, {"entity_type": "facilities", "batch": batch})
    assert result.get("status") in ("ok", "success"), result


async def test_good_pairs_outscore_bad_pairs_on_the_engine(benchmark_pack, record_property) -> None:
    await _sync_all()
    good = {pair.label: await _engine_score(pair) for pair in GOOD_PAIRS}
    bad = {pair.label: await _engine_score(pair) for pair in BAD_PAIRS}
    record_property("good_scores", good)
    record_property("bad_scores", bad)

    for label, score in {**good, **bad}.items():
        assert 0.0 <= score <= 1.0, (label, score)
    separation = statistics.mean(good.values()) - statistics.mean(bad.values())
    record_property("separation", separation)
    assert separation > _SEPARATION, f"good-vs-bad separation {separation:.4f} is not above {_SEPARATION}"


async def test_every_good_pair_outscores_every_bad_pair(benchmark_pack) -> None:
    await _sync_all()
    good = [await _engine_score(pair) for pair in GOOD_PAIRS]
    bad = [await _engine_score(pair) for pair in BAD_PAIRS]
    assert min(good) > max(bad), {"good": good, "bad": bad}


async def test_scores_discriminate(benchmark_pack) -> None:
    """The scores spread: the benchmark is not satisfied by a constant score."""
    await _sync_all()
    scores = [await _engine_score(pair) for pair in GOOD_PAIRS + BAD_PAIRS]
    assert statistics.stdev(scores) > 0.05, scores
