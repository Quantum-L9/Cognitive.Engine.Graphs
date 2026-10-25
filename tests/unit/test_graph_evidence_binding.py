"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, evidence, graph, reader, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_graph_evidence_binding.py
Unit tests for QM.POC0.CEG.GRAPH_EVIDENCE.002A's read side without Neo4j: the
graph evidence binding's failure modes (engine/scoring/graph_evidence.py) and
handle_score answering ``blocked`` when the binding cannot read. The real-graph
proof is tests/integration/test_graph_evidence_reader.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.graph.driver import DatabaseNotProvisionedError
from engine.handlers import handle_score
from engine.scoring.evidence_priority import EvidenceRead, EvidenceUnavailableError, approved_relevance_map
from engine.scoring.graph_evidence import MAX_EVIDENCE_EDGES, GraphEvidenceBinding, _is_stale, _single_revision
from engine.state import get_state

pytestmark = pytest.mark.unit


# ── Graph evidence binding failure modes ───────────────────────────────────────


def _driver(rows: list[dict[str, Any]] | None = None, exc: Exception | None = None) -> MagicMock:
    driver = MagicMock()
    driver.execute_read = AsyncMock(return_value=rows or [], side_effect=exc)
    driver.execute_query = AsyncMock(side_effect=AssertionError("score read must not use execute_query"))
    return driver


async def test_binding_reads_through_execute_read_only() -> None:
    driver = _driver([])
    read = await GraphEvidenceBinding(driver, approved_relevance_map({})).read(database="tenant-a", entity_refs=["a:1"])
    assert read == EvidenceRead(connected=True, database="tenant-a", observations=())
    driver.execute_read.assert_awaited_once()
    assert driver.execute_read.await_args.kwargs["database"] == "tenant-a"
    assert driver.execute_read.await_args.args[1]["tenant"] == "tenant-a"


async def test_missing_database_is_unavailable_not_empty() -> None:
    driver = _driver(exc=DatabaseNotProvisionedError("Neo4j database 'tenant-a' does not exist"))
    with pytest.raises(EvidenceUnavailableError, match="never creates it"):
        await GraphEvidenceBinding(driver, {}).read(database="tenant-a", entity_refs=["a:1"])


async def test_unconnected_driver_is_unavailable() -> None:
    driver = _driver()
    driver.get_driver.side_effect = RuntimeError("Driver not connected")
    with pytest.raises(EvidenceUnavailableError, match="not connected"):
        await GraphEvidenceBinding(driver, {}).read(database="tenant-a", entity_refs=["a:1"])


async def test_oversized_evidence_read_is_refused_not_truncated() -> None:
    row = {"entity_ref": "a:1", "relationship_type": "R", "props": {"evidence_ref": "e"}}
    driver = _driver([row] * (MAX_EVIDENCE_EDGES + 1))
    with pytest.raises(EvidenceUnavailableError, match="truncated"):
        await GraphEvidenceBinding(driver, {}).read(database="tenant-a", entity_refs=["a:1"])


async def test_malformed_stored_edges_are_skipped_and_status_maps_to_admission() -> None:
    def edge(ref: str, **props: Any) -> dict[str, Any]:
        base = {
            "evidence_ref": ref,
            "source_ref": "s",
            "confidence": 0.5,
            "assertion_status": "observed",
            "polarity": "positive",
        }
        return {"entity_ref": "a:1", "relationship_type": "R", "props": {**base, **props}}

    driver = _driver([edge("e:1"), edge("e:2", assertion_status="reported"), edge("e:3", polarity="sideways")])
    read = await GraphEvidenceBinding(driver, {}).read(database="tenant-a", entity_refs=["a:1"])
    assert [(o.evidence_ref, o.admission) for o in read.observations] == [
        ("e:1", "admitted"),
        ("e:2", "inferred_unverified"),
    ]


def test_staleness_and_revision_are_observed_never_invented() -> None:
    now = datetime.now(tz=UTC)
    assert _is_stale(None, now) is False
    assert _is_stale((now + timedelta(days=1)).isoformat(), now) is False
    assert _is_stale((now - timedelta(seconds=1)).isoformat(), now) is True
    assert _is_stale("2026-01-01T00:00:00", now) is True  # no offset: not trusted
    assert _is_stale("garbage", now) is True
    assert _single_revision([]) is None
    assert _single_revision([{"props": {"source_revision": "r1"}}, {"props": {"source_revision": "r1"}}]) == "r1"
    assert _single_revision([{"props": {"source_revision": "r1"}}, {"props": {"source_revision": "r2"}}]) is None
    assert _single_revision([{"props": {}}]) is None


# ── Handler seams ──────────────────────────────────────────────────────────────


@pytest.fixture
def _clean_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


@pytest.mark.usefixtures("_clean_state")
async def test_handle_score_is_blocked_when_the_binding_cannot_read() -> None:
    binding = GraphEvidenceBinding(_driver(exc=DatabaseNotProvisionedError("absent")), approved_relevance_map({}))
    result = await handle_score("tenant-a", {"entity_refs": ["a:1"], "top_n": 1}, binding=binding)
    assert result["status"] == "blocked"
    assert result["evidence_reader"]["connected"] is False
    assert result["scored"] == []
