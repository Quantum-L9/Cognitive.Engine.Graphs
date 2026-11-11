"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, causal, serializer, bfs]
owner: engine-team
status: active
--- /L9_META ---

Tests for engine.causal.serializer — CausalSubgraphSerializer.

Covers:
- BFS traversal returns serialized string
- Empty neighborhood returns empty result
- Correct node/edge counting
- Error handling on query failure
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.causal.serializer import CausalSubgraphSerializer
from engine.config.schema import (
    CausalEdgeSpec,
    CausalSubgraphSpec,
    DomainSpec,
)

pytestmark = pytest.mark.unit


def _spec_with_causal(enabled: bool = True) -> DomainSpec:
    return DomainSpec(
        domain={"id": "test", "name": "Test", "version": "0.0.1"},
        ontology={
            "nodes": [
                {
                    "label": "Facility",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "facility_id", "type": "int", "required": True}],
                },
                {
                    "label": "Query",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "query_id", "type": "int", "required": True}],
                },
                {
                    "label": "TransactionOutcome",
                    "managedby": "api",
                    "auxiliary": True,
                    "properties": [{"name": "outcome_id", "type": "string"}],
                },
            ],
            "edges": [
                {
                    "type": "TRANSACTED_WITH",
                    "from": "Facility",
                    "to": "Facility",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "sync",
                },
                {
                    "type": "RESULTED_IN",
                    "from": "Facility",
                    "to": "TransactionOutcome",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "api",
                },
            ],
        },
        matchentities={
            "candidate": [{"label": "Facility", "matchdirection": "d1"}],
            "queryentity": [{"label": "Query", "matchdirection": "d1"}],
        },
        queryschema={"matchdirections": ["d1"], "fields": []},
        gates=[],
        scoring={"dimensions": []},
        causal=CausalSubgraphSpec(
            enabled=enabled,
            causal_edges=[
                CausalEdgeSpec(
                    edge_type="RESULTED_IN",
                    source_label="Facility",
                    target_label="TransactionOutcome",
                ),
            ],
            chain_depth_limit=3,
        ),
    )


class TestCausalSubgraphSerializer:
    """Tests for BFS serialization."""

    @pytest.mark.asyncio
    async def test_serialize_returns_path_string(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        serializer = CausalSubgraphSerializer(driver, spec)

        driver.execute_query = AsyncMock(
            return_value=[
                {
                    "path_nodes": [
                        {"id": "F-1", "label": "Facility", "name": "Plant A"},
                        {"id": "TO-1", "label": "TransactionOutcome", "name": "TO-1"},
                    ],
                    "path_rels": [
                        {"type": "RESULTED_IN", "confidence": 0.9},
                    ],
                    "depth": 1,
                }
            ]
        )

        result = await serializer.serialize_neighborhood("F-1", "Facility")
        assert result["nodes_visited"] == 2
        assert "RESULTED_IN" in result["edges_traversed"]
        assert result["depth_reached"] == 1
        assert "Facility[Plant A]" in result["serialized"]
        assert "RESULTED_IN" in result["serialized"]

    @pytest.mark.asyncio
    async def test_empty_neighborhood(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        serializer = CausalSubgraphSerializer(driver, spec)

        driver.execute_query = AsyncMock(return_value=[])

        result = await serializer.serialize_neighborhood("F-999", "Facility")
        assert result["serialized"] == ""
        assert result["nodes_visited"] == 0
        assert result["edges_traversed"] == []
        assert result["depth_reached"] == 0

    @pytest.mark.asyncio
    async def test_query_failure_returns_empty(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        serializer = CausalSubgraphSerializer(driver, spec)

        driver.execute_query = AsyncMock(side_effect=Exception("Neo4j connection lost"))

        result = await serializer.serialize_neighborhood("F-1", "Facility")
        assert result["serialized"] == ""
        assert result["nodes_visited"] == 0

    @pytest.mark.asyncio
    async def test_multi_hop_path(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        serializer = CausalSubgraphSerializer(driver, spec)

        driver.execute_query = AsyncMock(
            return_value=[
                {
                    "path_nodes": [
                        {"id": "F-1", "label": "Facility", "name": "Plant A"},
                        {"id": "TO-1", "label": "TransactionOutcome", "name": "TO-1"},
                    ],
                    "path_rels": [{"type": "RESULTED_IN", "confidence": 0.9}],
                    "depth": 1,
                },
                {
                    "path_nodes": [
                        {"id": "F-1", "label": "Facility", "name": "Plant A"},
                        {"id": "F-2", "label": "Facility", "name": "Plant B"},
                    ],
                    "path_rels": [{"type": "RESULTED_IN", "confidence": 0.7}],
                    "depth": 1,
                },
            ]
        )

        result = await serializer.serialize_neighborhood("F-1", "Facility", max_depth=2)
        assert result["nodes_visited"] >= 2
        assert result["depth_reached"] == 1


class TestDeclaredIdProperty:
    """The BFS keys on the domain's declared id, not a fixed ``entity_id``."""

    @pytest.mark.asyncio
    async def test_declared_id_property_drives_the_query(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=[])
        serializer = CausalSubgraphSerializer(driver, spec)

        await serializer.serialize_neighborhood("D-1", "Depot", id_property="depot_code")

        cypher = driver.execute_query.call_args.args[0]
        assert "MATCH (start:Depot)" in cypher
        assert "WHERE start.depot_code = $node_id" in cypher
        assert "id: n.depot_code" in cypher
        assert "coalesce(n.name, n.depot_code)" in cypher
        assert "entity_id" not in cypher
        assert driver.execute_query.call_args.kwargs["parameters"]["node_id"] == "D-1"

    @pytest.mark.asyncio
    async def test_default_id_property_is_entity_id(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=[])
        serializer = CausalSubgraphSerializer(driver, spec)

        await serializer.serialize_neighborhood("F-1", "Facility")

        cypher = driver.execute_query.call_args.args[0]
        assert "WHERE start.entity_id = $node_id" in cypher
        assert "id: n.entity_id" in cypher

    @pytest.mark.asyncio
    async def test_unsafe_id_property_is_refused_before_execution(self) -> None:
        spec = _spec_with_causal()
        driver = AsyncMock()
        serializer = CausalSubgraphSerializer(driver, spec)

        with pytest.raises(ValueError, match="Invalid label"):
            await serializer.serialize_neighborhood("F-1", "Facility", id_property="id} RETURN 1 //")
        driver.execute_query.assert_not_called()


class TestVariableDepth:
    """RU-074 / CEG-COR-121: the pattern is variable-length up to max_depth."""

    async def _cypher(self, max_depth, spec=None) -> tuple[str, dict]:
        driver = AsyncMock()
        driver.execute_query = AsyncMock(return_value=[])
        await CausalSubgraphSerializer(driver, spec or _spec_with_causal()).serialize_neighborhood(
            "F-1", "Facility", max_depth=max_depth, id_property="facility_id"
        )
        return driver.execute_query.call_args.args[0], driver.execute_query.call_args.kwargs["parameters"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("depth", [1, 2, 3])
    async def test_pattern_is_variable_length(self, depth: int) -> None:
        cypher, params = await self._cypher(depth)
        assert f"(start)-[:RESULTED_IN*1..{depth}]->(neighbor)" in cypher
        assert "[r:" not in cypher
        assert params["max_depth"] == depth
        assert "LIMIT $max_nodes" in cypher

    @pytest.mark.asyncio
    async def test_depth_is_bounded_by_chain_depth_limit(self) -> None:
        cypher, params = await self._cypher(5)  # chain_depth_limit=3 in the fixture
        assert "*1..3]" in cypher
        assert params["max_depth"] == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("depth", [0, -4])
    async def test_non_positive_depth_clamps_to_one(self, depth: int) -> None:
        cypher, _params = await self._cypher(depth)
        assert "*1..1]" in cypher
        assert "*1..0" not in cypher
        assert "*1..-" not in cypher

    @pytest.mark.asyncio
    async def test_no_causal_edges_uses_untyped_variable_length(self) -> None:
        spec = _spec_with_causal()
        spec = spec.model_copy(update={"causal": spec.causal.model_copy(update={"causal_edges": []})})
        cypher, _params = await self._cypher(2, spec)
        assert "(start)-[*1..2]->(neighbor)" in cypher

    @pytest.mark.asyncio
    async def test_flag_off_restores_fixed_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "causal_serializer_variable_depth", False)
        cypher, _params = await self._cypher(2)
        assert "(start)-[r:RESULTED_IN]->(neighbor)" in cypher
