"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [causal, outcomes, integration]
owner: engine-team
status: active
--- /L9_META ---

CEG-FIX-001 — a rejected RESULTED_IN edge must never reach the graph.

handle_outcomes runs CausalEdgeRuntimeValidator (R3) over the outcome's
RESULTED_IN edge record when ``domain_spec.causal.enabled``. These tests drive
the real handler against a real Neo4j and read the resulting graph back, so
they prove graph effects rather than the shape of the Cypher that was sent.

The validator judges the edge, not the outcome observation
(docs/contracts/data/graph-schema.yaml: "RESULTED_IN … Validated by
CausalEdgeRuntimeValidator (R3)"), so a rejection suppresses the edge and the
TransactionOutcome node is still recorded.

The domain database is ``neo4j`` so the suite runs against both the community
server and the enterprise testcontainer.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from engine.config.schema import DomainSpec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.finding("CEG-FIX-001")]

_DB = "neo4j"
_TENANT = "neo4j"
_RUN_TAG = "ceg_fix_001"


_RESULTED_IN = {
    "edge_type": "RESULTED_IN",
    "source_label": "Facility",
    "target_label": "TransactionOutcome",
    "temporal_validation": True,
    "confidence_threshold": 0.5,
}


def _domain_spec(base: DomainSpec, *, causal_enabled: bool = True) -> DomainSpec:
    """The shared minimal spec, bound to the ``neo4j`` database, with R3 on RESULTED_IN."""
    raw = base.model_dump(by_alias=True)
    raw["domain"]["id"] = _DB
    raw["causal"] = {"enabled": causal_enabled, "causal_edges": [_RESULTED_IN]}
    return DomainSpec.model_validate(raw)


class _Loader:
    def __init__(self, spec: DomainSpec) -> None:
        self._spec = spec

    def load_domain(self, _tenant: str) -> DomainSpec:
        return self._spec


class _Wire:
    """One test's isolated slice of the shared graph: its own candidate and tag."""

    def __init__(self, graph_driver: Any, tag: str, base: DomainSpec) -> None:
        self._driver = graph_driver
        self.tag = tag
        self._base = base

    def spec(self, *, causal_enabled: bool = True) -> DomainSpec:
        return _domain_spec(self._base, causal_enabled=causal_enabled)

    async def bind(self, spec: DomainSpec) -> None:
        from engine.handlers import init_dependencies
        from engine.state import get_state

        get_state().reset()
        init_dependencies(self._driver, _Loader(spec))

    def payload(self, **causal: Any) -> dict[str, Any]:
        return {
            "match_id": f"{self.tag}_match",
            "candidate_id": f"{self.tag}_cand",
            "outcome": "success",
            **causal,
        }


@pytest_asyncio.fixture
async def wire(graph_driver, minimal_domain_spec) -> AsyncGenerator[_Wire, None]:
    """Bind the real handler DI to the real driver; seed and clean one candidate.

    Every test gets a unique tag so parallel workers sharing one server never
    see, or delete, each other's nodes.
    """
    from engine.state import get_state

    tag = f"{_RUN_TAG}_{uuid.uuid4().hex[:12]}"
    await graph_driver.execute_write(
        # RU-002: outcome edges link by the declared id (facility_id); entity_id
        # stays so the SPEC_DECLARED_CANDIDATE_ID=false rollback still links.
        cypher="CREATE (c:Facility {facility_id: $cid, entity_id: $cid, run_tag: $tag})",
        parameters={"cid": f"{tag}_cand", "tag": tag},
        database=_DB,
    )
    yield _Wire(graph_driver, tag, minimal_domain_spec)
    get_state().reset()
    await graph_driver.execute_write(
        cypher=("MATCH (n) WHERE n.run_tag = $tag OR (n:TransactionOutcome AND n.match_id = $match) DETACH DELETE n"),
        parameters={"tag": tag, "match": f"{tag}_match"},
        database=_DB,
    )


async def _outcome_graph(graph_driver, outcome_id: str) -> dict[str, Any]:
    rows = await graph_driver.execute_query(
        cypher=(
            "MATCH (o:TransactionOutcome {outcome_id: $oid}) "
            "OPTIONAL MATCH (c)-[r:RESULTED_IN]->(o) "
            "RETURN o.tenant AS tenant, o.domain_id AS domain_id, count(r) AS edges"
        ),
        parameters={"oid": outcome_id},
        database=_DB,
    )
    return rows[0] if rows else {}


@pytest.mark.asyncio
async def test_temporally_invalid_edge_is_not_persisted(wire, graph_driver) -> None:
    from engine.handlers import handle_outcomes

    await wire.bind(wire.spec())
    result = await handle_outcomes(
        _TENANT,
        wire.payload(
            source_ts="2026-03-02T00:00:00+00:00",
            target_ts="2026-03-01T00:00:00+00:00",
            confidence=0.9,
        ),
    )

    graph = await _outcome_graph(graph_driver, result["outcome_id"])
    assert graph, "the outcome observation itself must still be recorded"
    assert graph["edges"] == 0, "a temporally invalid RESULTED_IN edge was persisted"
    assert result["status"] == "recorded"
    assert result["rejected_count"] == 1


@pytest.mark.asyncio
async def test_below_threshold_edge_is_not_persisted(wire, graph_driver) -> None:
    from engine.handlers import handle_outcomes

    await wire.bind(wire.spec())
    result = await handle_outcomes(
        _TENANT,
        wire.payload(
            source_ts="2026-03-01T00:00:00+00:00",
            target_ts="2026-03-02T00:00:00+00:00",
            confidence=0.2,
        ),
    )

    graph = await _outcome_graph(graph_driver, result["outcome_id"])
    assert graph
    assert graph["edges"] == 0, "a below-threshold RESULTED_IN edge was persisted"
    assert result["rejected_count"] == 1


@pytest.mark.asyncio
async def test_valid_edge_is_persisted_once(wire, graph_driver) -> None:
    from engine.handlers import handle_outcomes

    await wire.bind(wire.spec())
    result = await handle_outcomes(
        _TENANT,
        wire.payload(
            source_ts="2026-03-01T00:00:00+00:00",
            target_ts="2026-03-02T00:00:00+00:00",
            confidence=0.9,
        ),
    )

    graph = await _outcome_graph(graph_driver, result["outcome_id"])
    assert graph["edges"] == 1
    assert graph["tenant"] == _TENANT
    assert graph["domain_id"] == _DB
    assert "rejected_count" not in result


@pytest.mark.asyncio
async def test_causal_disabled_keeps_unvalidated_edge(wire, graph_driver) -> None:
    """With causal off the validator is not consulted — existing behaviour."""
    from engine.handlers import handle_outcomes

    await wire.bind(wire.spec(causal_enabled=False))
    result = await handle_outcomes(
        _TENANT,
        wire.payload(
            source_ts="2026-03-02T00:00:00+00:00",
            target_ts="2026-03-01T00:00:00+00:00",
            confidence=0.1,
        ),
    )

    graph = await _outcome_graph(graph_driver, result["outcome_id"])
    assert graph["edges"] == 1
    assert "rejected_count" not in result


@pytest.mark.asyncio
async def test_enforcement_flag_off_restores_log_only(wire, graph_driver, monkeypatch) -> None:
    """CAUSAL_REJECTION_ENFORCEMENT_ENABLED=false is the operator rollback."""
    from engine.config.settings import settings
    from engine.handlers import handle_outcomes

    monkeypatch.setattr(settings, "causal_rejection_enforcement_enabled", False)
    await wire.bind(wire.spec())
    result = await handle_outcomes(
        _TENANT,
        wire.payload(
            source_ts="2026-03-01T00:00:00+00:00",
            target_ts="2026-03-02T00:00:00+00:00",
            confidence=0.2,
        ),
    )

    graph = await _outcome_graph(graph_driver, result["outcome_id"])
    assert graph["edges"] == 1
    assert "rejected_count" not in result
