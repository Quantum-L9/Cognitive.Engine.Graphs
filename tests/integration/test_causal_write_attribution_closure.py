"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [causal, compiler, attribution, integration]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 N5 (cases C21, C22, C23, C25, C26) on a real Neo4j.

The compiled causal edge write is executed and read back (declared identity,
idempotent MERGE, threshold, temporal precedence); attribution is computed
over a graph with repeated paths and a cycle, and over two tenant databases
that share outcome/touchpoint ids.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from engine.causal.attribution import AttributionCalculator
from engine.causal.causal_compiler import CausalCompiler
from engine.causal.causal_validator import CausalEdgeRuntimeValidator
from engine.config.schema import CausalEdgeSpec, DomainSpec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.finding("F11")]

_DB = "neo4j"
_SHADOW_DB = "causal-closure-shadow"
_RESULTED_IN = {
    "edge_type": "RESULTED_IN",
    "source_label": "Widget",
    "target_label": "WidgetOutcome",
    "temporal_validation": True,
    "confidence_threshold": 0.5,
}
_CONTRIBUTED_TO = {
    "edge_type": "CONTRIBUTED_TO",
    "source_label": "Widget",
    "target_label": "Widget",
    "temporal_validation": False,
}


def _spec(base: DomainSpec, database: str = _DB) -> DomainSpec:
    raw = base.model_dump(by_alias=True)
    raw["domain"]["id"] = database
    raw["causal"] = {"enabled": True, "causal_edges": [_RESULTED_IN, _CONTRIBUTED_TO], "chain_depth_limit": 4}
    raw["feedbackloop"] = {**raw.get("feedbackloop", {}), "outcome_node_label": "WidgetOutcome"}
    return DomainSpec.model_validate(raw)


@pytest_asyncio.fixture
async def tag(graph_driver) -> AsyncGenerator[str, None]:
    value = f"causal_closure_{uuid.uuid4().hex[:12]}"
    yield value
    for database in (_DB, _SHADOW_DB):
        try:
            await graph_driver.execute_write(
                cypher="MATCH (n) WHERE n.run_tag = $tag DETACH DELETE n",
                parameters={"tag": value},
                database=database,
            )
        except Exception:  # the shadow database exists only after the tenant test ran
            continue


async def _edges(driver: Any, tag: str, database: str = _DB) -> list[dict[str, Any]]:
    return await driver.execute_query(
        cypher=(
            "MATCH (s {run_tag: $tag})-[r]->(t {run_tag: $tag}) "
            "RETURN type(r) AS type, s.widget_id AS source, t.outcome_id AS target, r.confidence AS confidence"
        ),
        parameters={"tag": tag},
        database=database,
    )


async def _seed_pair(driver: Any, tag: str, *, outcome_later: bool = True) -> None:
    order = "datetime() - duration('P1D')" if outcome_later else "datetime() + duration('P1D')"
    await driver.execute_write(
        cypher=(
            f"CREATE (:Widget {{widget_id: $wid, run_tag: $tag, created_at: {order}}}) "
            "CREATE (:WidgetOutcome {outcome_id: $oid, run_tag: $tag, created_at: datetime()})"
        ),
        parameters={"wid": f"{tag}_w", "oid": f"{tag}_o", "tag": tag},
        database=_DB,
    )


@pytest.mark.asyncio
async def test_valid_edge_merged_once_by_declared_ids(graph_driver, minimal_domain_spec, tag) -> None:
    """C25 (valid half): a repeated valid write MERGEs one edge, matched by widget_id / outcome_id."""
    spec = _spec(minimal_domain_spec)
    cypher = CausalCompiler(spec).compile_causal_edge_create(CausalEdgeSpec(**_RESULTED_IN))
    await _seed_pair(graph_driver, tag)
    params = {
        "source_id": f"{tag}_w",
        "target_id": f"{tag}_o",
        "mechanism": "m",
        "confidence_threshold": 0.5,
    }

    await graph_driver.execute_write(cypher=cypher, parameters={**params, "confidence": 0.7}, database=_DB)
    await graph_driver.execute_write(cypher=cypher, parameters={**params, "confidence": 0.9}, database=_DB)

    edges = await _edges(graph_driver, tag)
    assert edges == [{"type": "RESULTED_IN", "source": f"{tag}_w", "target": f"{tag}_o", "confidence": 0.9}]


@pytest.mark.asyncio
async def test_low_confidence_and_inverted_time_write_nothing(graph_driver, minimal_domain_spec, tag) -> None:
    """C25 (refusal half) / C23: below-threshold and temporally inverted edges never reach the graph."""
    spec = _spec(minimal_domain_spec)
    cypher = CausalCompiler(spec).compile_causal_edge_create(CausalEdgeSpec(**_RESULTED_IN))
    params = {"source_id": f"{tag}_w", "target_id": f"{tag}_o", "mechanism": "m", "confidence_threshold": 0.5}

    await _seed_pair(graph_driver, tag)
    await graph_driver.execute_write(cypher=cypher, parameters={**params, "confidence": 0.2}, database=_DB)
    assert await _edges(graph_driver, tag) == []

    inverted = f"{tag}_inv"
    await _seed_pair(graph_driver, inverted, outcome_later=False)
    await graph_driver.execute_write(
        cypher=(
            "MATCH (n) WHERE n.run_tag = $inv SET n.run_tag = $tag"  # fold into this test's cleanup tag
        ),
        parameters={"inv": inverted, "tag": tag},
        database=_DB,
    )
    await graph_driver.execute_write(
        cypher=cypher,
        parameters={**params, "source_id": f"{inverted}_w", "target_id": f"{inverted}_o", "confidence": 0.9},
        database=_DB,
    )
    assert await _edges(graph_driver, tag) == []
    rows = await graph_driver.execute_query(
        cypher="MATCH (o:WidgetOutcome {outcome_id: $oid}) RETURN count(o) AS n",
        parameters={"oid": f"{inverted}_o"},
        database=_DB,
    )
    assert rows[0]["n"] == 1, "the outcome observation is retained"


@pytest.mark.asyncio
async def test_undeclared_edge_type_is_refused_and_not_written(graph_driver, minimal_domain_spec, tag) -> None:
    """C22: an undeclared causal type is rejected by the validator; the compiler writes only declared types."""
    spec = _spec(minimal_domain_spec)
    valid, rejected = CausalEdgeRuntimeValidator(spec.causal).validate_batch(
        [{"source_ts": "2026-01-01T00:00:00+00:00", "target_ts": "2026-01-02T00:00:00+00:00", "confidence": 0.9}],
        "CAUSED_BY_MAGIC",
    )
    assert valid == []
    assert rejected[0]["rejection_reason"] == "invalid_edge_type"

    await _seed_pair(graph_driver, tag)
    compiled = CausalCompiler(spec).compile_all_edge_creates()
    assert {edge.edge_type for edge, _ in compiled} == {"RESULTED_IN", "CONTRIBUTED_TO"}
    for _edge, cypher in compiled:
        await graph_driver.execute_write(
            cypher=cypher,
            parameters={
                "source_id": f"{tag}_w",
                "target_id": f"{tag}_o",
                "confidence": 0.9,
                "mechanism": "m",
                "confidence_threshold": 0.5,
            },
            database=_DB,
        )
    assert {e["type"] for e in await _edges(graph_driver, tag)} <= {"RESULTED_IN", "CONTRIBUTED_TO"}


_CHAIN = """
CREATE (w1:Widget {widget_id: $p + 'w1', run_tag: $tag})
CREATE (w2:Widget {widget_id: $p + 'w2', run_tag: $tag})
CREATE (w3:Widget {widget_id: $p + 'w3', run_tag: $tag})
CREATE (o:WidgetOutcome {outcome_id: $oid, run_tag: $tag})
CREATE (w1)-[:CONTRIBUTED_TO {confidence: 0.9}]->(w2)
CREATE (w2)-[:CONTRIBUTED_TO {confidence: 0.9}]->(w1)
CREATE (w2)-[:CONTRIBUTED_TO {confidence: 0.9}]->(w3)
CREATE (w1)-[:RESULTED_IN {confidence: 0.9}]->(o)
CREATE (w3)-[:RESULTED_IN {confidence: 0.9}]->(o)
CREATE (w1)-[:RESULTED_IN {confidence: 0.4}]->(o)
"""


@pytest.mark.parametrize("model", ["first_touch", "last_touch", "linear", "position_based"])
@pytest.mark.asyncio
async def test_attribution_over_repeats_and_cycles(graph_driver, minimal_domain_spec, tag, model: str) -> None:
    """C21: unique touchpoints, bounded weights summing to 1, under parallel edges and a cycle."""
    oid = f"{tag}_o"
    await graph_driver.execute_write(cypher=_CHAIN, parameters={"p": tag, "oid": oid, "tag": tag}, database=_DB)

    result = await AttributionCalculator(graph_driver, _spec(minimal_domain_spec)).compute_attribution(oid, model)

    touchpoints = result["touchpoints"]
    assert set(touchpoints) == {f"{tag}w1", f"{tag}w2", f"{tag}w3"}
    assert oid not in touchpoints
    assert result["total_touchpoints"] == 3
    assert all(0.0 <= w <= 1.0 for w in touchpoints.values())
    assert sum(touchpoints.values()) == pytest.approx(1.0, abs=1e-5)


@pytest.mark.asyncio
async def test_attribution_stays_in_tenant_database(graph_driver, minimal_domain_spec, tag) -> None:
    """C26: the same outcome/touchpoint ids in two tenant databases never cross."""
    await graph_driver.execute_query(
        cypher=f"CREATE DATABASE `{_SHADOW_DB}` IF NOT EXISTS WAIT", parameters={}, database="system"
    )
    oid = f"{tag}_o"
    seed = (
        "CREATE (w:Widget {widget_id: $wid, run_tag: $tag}) "
        "CREATE (o:WidgetOutcome {outcome_id: $oid, run_tag: $tag}) "
        "CREATE (w)-[:RESULTED_IN {confidence: 0.9}]->(o)"
    )
    await graph_driver.execute_write(cypher=seed, parameters={"wid": "mine", "oid": oid, "tag": tag}, database=_DB)
    await graph_driver.execute_write(
        cypher=seed, parameters={"wid": "theirs", "oid": oid, "tag": tag}, database=_SHADOW_DB
    )

    mine = await AttributionCalculator(graph_driver, _spec(minimal_domain_spec)).compute_attribution(oid)
    theirs = await AttributionCalculator(graph_driver, _spec(minimal_domain_spec, _SHADOW_DB)).compute_attribution(oid)

    assert mine["touchpoints"] == {"mine": 1.0}
    assert theirs["touchpoints"] == {"theirs": 1.0}
