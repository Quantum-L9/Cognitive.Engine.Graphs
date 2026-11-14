"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, hoprag, config]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_hoprag_config.py
Unit tests for engine/hoprag/config.HopRAGConfig construction-time validation.

Findings covered:
  CEG-COR-176  index_batch_size < 1 is rejected like the sibling bounds, so
               GraphIndexBuilder pagination always advances

The indexer runs against an in-memory store with Python slice pagination
(rows[offset:offset+batch_size]). No Neo4j.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.settings import settings
from engine.hoprag.config import HopRAGConfig
from engine.hoprag.indexer import GraphIndexBuilder


@pytest.fixture(autouse=True)
def _unparked(monkeypatch: pytest.MonkeyPatch) -> None:
    """CEG-KGE-PARK: prove the parked mechanism under an explicit un-park.

    The default refusal is asserted in tests/unit/test_kge_parked.py.
    """
    monkeypatch.setattr(settings, "parked_subsystems_enabled", True)


pytestmark = pytest.mark.unit


class _SliceStore:
    """In-memory passage store; fails loudly instead of paginating forever."""

    def __init__(self, n: int) -> None:
        self.rows = [{"id": f"p{i}", "text": f"t{i}"} for i in range(n)]
        self.calls = 0

    async def get_vertex_count(self, label: str) -> int:
        return len(self.rows)

    async def fetch_passages(self, label: str, batch_size: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls > 50:
            msg = "non-terminating pagination"
            raise RuntimeError(msg)
        return self.rows[offset : offset + batch_size]

    async def write_edges(self, edges: list[Any], edge_type: str = "HOPRAG_EDGE") -> int:
        return len(edges)


class _NoQuestions:
    def generate_batch(self, passages: list[Any], n_incoming: int, m_outgoing: int) -> list[Any]:
        return []


@pytest.mark.finding("CEG-COR-176")
class TestIndexBatchSizeBound:
    @pytest.mark.parametrize("bad", [0, -5])
    def test_non_positive_batch_size_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match=f"index_batch_size must be >= 1, got {bad}"):
            HopRAGConfig(index_batch_size=bad)

    @pytest.mark.parametrize("bad", [0, -5])
    def test_from_dict_rejects_non_positive_batch_size(self, bad: int) -> None:
        with pytest.raises(ValueError, match="index_batch_size must be >= 1"):
            HopRAGConfig.from_dict({"index_batch_size": bad})

    def test_default_and_sibling_bounds_unchanged(self) -> None:
        assert HopRAGConfig().index_batch_size == 100
        assert HopRAGConfig(index_batch_size=1).index_batch_size == 1
        with pytest.raises(ValueError, match="n_hop must be >= 1"):
            HopRAGConfig(n_hop=0)

    @pytest.mark.parametrize("batch_size", [1, 3, 5, 10, 100])
    async def test_indexer_processes_every_passage_for_valid_sizes(self, batch_size: int) -> None:
        store = _SliceStore(10)
        result = await GraphIndexBuilder(
            HopRAGConfig(enabled=True, index_batch_size=batch_size), _NoQuestions(), store
        ).build()
        assert result.passages_processed == 10
        assert result.errors == []
