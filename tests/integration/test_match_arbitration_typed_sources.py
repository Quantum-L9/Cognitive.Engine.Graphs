"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, handlers, match, arbitration, scoring]
owner: engine-team
status: active
--- /L9_META ---

CEG-01 / CEG-02 (fourth external audit, 2026-11-27) against a real Neo4j.

Depots are written by ``handle_sync`` on the orbital-depots endpoint and read
by ``handle_match``; constraint knownness follows the scoring compiler's typed
source resolution. Each test overlays one extra dimension (weight 0.1, so the
pack's 0.6 + 0.3 stays within the C-22 ceiling) and one hard constraint on it.

* a native DateTime ``last_seen`` (``temporalproximity``) is a known source: a
  fresh depot scores recency 1.0 and passes ``>= 0.5``. ``handle_sync`` stores a
  declared datetime as its ISO string, so the native value is set afterwards by
  a direct write, as a GDS job or another writer would;
* a string ``community`` (``communitymatch``) is a known source: a member
  passes the membership constraint;
* a ``candidateproperty`` dimension that omits ``candidateprop`` reads
  ``value``; a depot without it scores ``defaultwhennull`` 0.8 and still fails
  ``>= 0.7``;
* a numeric 0 passes threshold 0 and a NaN fails it, in all four
  ``match_dimension_projection`` x ``score_clamp_enabled`` states.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.schema import (
    ArbitrationConstraintSpec,
    DecisionArbitrationSpec,
    PropertySpec,
    QueryFieldSpec,
    ScoringDimensionSpec,
)
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, serve

pytestmark = pytest.mark.integration


def _depot(depot_id: str, **extra: Any) -> dict[str, Any]:
    return {"depot_id": depot_id, "reliability": 0.9, "docking_ports": 100, "accepts_hazmat": True} | extra


def _overlay(
    spec: Any,
    dimension: dict[str, Any],
    threshold: float,
    *,
    properties: tuple[dict[str, Any], ...] = (),
    fields: tuple[dict[str, Any], ...] = (),
) -> Any:
    name = dimension["name"]
    dim = ScoringDimensionSpec(
        **({"source": "candidateproperty", "weightkey": f"w_{name}", "defaultweight": 0.1} | dimension)
    )
    nodes = [
        node.model_copy(update={"properties": [*node.properties, *(PropertySpec(**p) for p in properties)]})
        if node.label == "OrbitalDepot"
        else node
        for node in spec.ontology.nodes
    ]
    constraint = ArbitrationConstraintSpec(dimension=name, threshold=threshold, hard=True)
    return spec.model_copy(
        update={
            "ontology": spec.ontology.model_copy(update={"nodes": nodes}),
            "scoring": spec.scoring.model_copy(update={"dimensions": [*spec.scoring.dimensions, dim]}),
            "queryschema": spec.queryschema.model_copy(
                update={"fields": [*spec.queryschema.fields, *(QueryFieldSpec(**f) for f in fields)]}
            ),
            "decision_arbitration": DecisionArbitrationSpec(enabled=True, constraints=[constraint]),
        }
    )


async def _sync(depots: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(ORBITAL, {"entity_type": "orbital-depots", "batch": depots})
    assert result.get("status") in ("ok", "success"), result


async def _ids(query: dict[str, Any] | None = None) -> list[str]:
    from engine.handlers import handle_match

    result = await handle_match(ORBITAL, {"query": query or {}, "match_direction": DIRECTION, "top_n": 10})
    return sorted(row["candidate"]["depot_id"] for row in result["candidates"])


@pytest.mark.asyncio
async def test_native_datetime_source_fresh_passes(engine_deps, graph_driver, domain_spec, clean_db, monkeypatch):
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "temporalproximity_declared_signals_enabled", True)  # pure recency decay
    await _sync([_depot("fresh"), _depot("never")])
    await graph_driver.execute_write(
        cypher="MATCH (n:OrbitalDepot {depot_id: 'fresh'}) SET n.last_seen = datetime()",
        parameters={},
        database=ORBITAL,
    )
    recency = {"name": "recency", "computation": "temporalproximity", "candidateprop": "last_seen", "maxvalue": 90}
    serve(graph_driver, _overlay(domain_spec, recency, 0.5))

    assert await _ids() == ["fresh"]  # never-seen has no source: unknown, fails


@pytest.mark.asyncio
async def test_string_community_source_passes_membership(engine_deps, graph_driver, domain_spec, clean_db):
    cohort = {"name": "cohort", "computation": "communitymatch", "candidateprop": "community"}
    spec = _overlay(
        domain_spec,
        cohort,
        0.9,
        properties=({"name": "community", "type": "string"},),
        fields=({"name": "community", "type": "string"},),
    )
    serve(graph_driver, spec)
    await _sync([_depot("member", community="c-7"), _depot("outsider", community="c-2"), _depot("unassigned")])

    assert await _ids({"community": "c-7"}) == ["member"]


@pytest.mark.asyncio
async def test_omitted_candidateprop_missing_default_source_fails(engine_deps, graph_driver, domain_spec, clean_db):
    fallback = {"name": "fallback", "computation": "candidateproperty", "defaultwhennull": 0.8}
    spec = _overlay(domain_spec, fallback, 0.7, properties=({"name": "value", "type": "float"},))
    serve(graph_driver, spec)
    await _sync([_depot("stored", value=0.8), _depot("defaulted")])

    # defaulted scores defaultwhennull 0.8 on the fallback dimension, but `value` is missing.
    assert await _ids() == ["stored"]


@pytest.fixture(
    params=[(True, True), (True, False), (False, True), (False, False)],
    ids=["projection_on-clamp_on", "projection_on-clamp_off", "projection_off-clamp_on", "projection_off-clamp_off"],
)
def flag_state(request, monkeypatch):
    from engine.config.settings import settings

    projection, clamp = request.param
    monkeypatch.setattr(settings, "match_dimension_projection", projection)
    monkeypatch.setattr(settings, "score_clamp_enabled", clamp)
    return request.param


@pytest.mark.asyncio
async def test_numeric_zero_passes_and_nan_fails(engine_deps, graph_driver, domain_spec, clean_db, flag_state):
    reliability = {"name": "known_reliability", "computation": "candidateproperty", "candidateprop": "reliability"}
    serve(graph_driver, _overlay(domain_spec, reliability, 0.0))
    await _sync([_depot("zero", reliability=0.0), _depot("finite")])
    await graph_driver.execute_write(
        cypher="MERGE (n:OrbitalDepot {depot_id: 'nan'}) "
        "SET n.reliability = $nan, n.docking_ports = 100, n.accepts_hazmat = true",
        parameters={"nan": float("nan")},
        database=ORBITAL,
    )

    assert await _ids() == ["finite", "zero"]
