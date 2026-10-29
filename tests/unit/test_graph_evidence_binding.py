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
from neo4j.exceptions import (
    ClientError,
    ConstraintError,
    CypherSyntaxError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from engine.graph.circuit_breaker import BreakerState, CircuitBreaker, CircuitOpenError
from engine.graph.driver import DatabaseNotProvisionedError, ExpectedWriteOutcome, GraphDriver
from engine.handlers import handle_score
from engine.scoring.evidence_priority import EvidenceRead, EvidenceUnavailableError, approved_relevance_map
from engine.scoring.graph_evidence import (
    MAX_EVIDENCE_EDGES,
    GraphEvidenceBinding,
    _is_stale,
    _single_revision,
    declared_evidence_types,
)
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


@pytest.mark.parametrize(
    "outage",
    [
        CircuitOpenError("neo4j", retry_after=5.0),
        ServiceUnavailable("connection refused"),
        SessionExpired("session expired"),
        TransientError("database unavailable"),
        TimeoutError("read timed out"),
        ClientError("failed to obtain a connection from the pool within 60.0s (timeout)"),
    ],
    ids=["circuit-open", "service-unavailable", "session-expired", "transient", "timeout", "pool-timeout"],
)
async def test_graph_outages_are_unavailable_so_the_score_is_blocked(outage: Exception) -> None:
    with pytest.raises(EvidenceUnavailableError, match="unavailable"):
        await GraphEvidenceBinding(_driver(exc=outage), {}).read(database="tenant-a", entity_refs=["a:1"])


@pytest.mark.parametrize(
    "defect",
    [CypherSyntaxError("Invalid input"), ClientError("unauthorized"), ValueError("explicit database="), KeyError("x")],
    ids=["cypher-syntax", "client-error", "value-error", "programming-error"],
)
async def test_wrong_queries_and_programming_errors_stay_visible(defect: Exception) -> None:
    with pytest.raises(type(defect)):
        await GraphEvidenceBinding(_driver(exc=defect), {}).read(database="tenant-a", entity_refs=["a:1"])


# ── Absent tenant databases are breaker-neutral (GraphDriver.execute_read) ─────

_ABSENT = Exception("Neo.ClientError.Database.DatabaseNotFound: Database does not exist. Database name: 'tenant-a'.")


def _breaker_driver(threshold: int = 3) -> GraphDriver:
    driver = GraphDriver(uri="bolt://unused:7687", username="u", password="p")
    driver._circuit_breaker = CircuitBreaker(name="neo4j", failure_threshold=threshold, recovery_timeout=0.0)
    return driver


async def _absent_read(driver: GraphDriver) -> None:
    with pytest.raises(DatabaseNotProvisionedError):
        await driver.execute_read("RETURN 1", {}, database="tenant-a")


async def _outage_read(driver: GraphDriver) -> None:
    with pytest.raises(ServiceUnavailable):
        await driver.execute_read("RETURN 1", {}, database="tenant-b")


async def test_repeated_absent_database_reads_leave_the_breaker_untouched() -> None:
    driver = _breaker_driver()
    driver._raw_execute_read = AsyncMock(side_effect=_ABSENT)  # type: ignore[method-assign]
    for _ in range(10):
        await _absent_read(driver)
    metrics = driver.circuit_breaker.get_metrics()
    assert metrics.state == "CLOSED"
    assert (metrics.total_failures, metrics.total_successes, metrics.consecutive_failures) == (0, 0, 0)


async def test_absent_reads_neither_mask_nor_reset_a_run_of_real_outages() -> None:
    driver = _breaker_driver(threshold=3)
    driver._raw_execute_read = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ServiceUnavailable("down"), _ABSENT, ServiceUnavailable("down"), _ABSENT, ServiceUnavailable("x")]
    )
    await _outage_read(driver)
    await _absent_read(driver)
    await _outage_read(driver)
    assert driver.circuit_breaker.get_metrics().consecutive_failures == 2  # the absent read did not reset it
    await _absent_read(driver)
    await _outage_read(driver)
    assert driver.circuit_breaker.state is BreakerState.OPEN  # three genuine outages still trip it


async def test_healthy_tenant_read_recovers_and_an_absent_probe_closes_a_half_open_breaker() -> None:
    driver = _breaker_driver(threshold=1)
    driver._raw_execute_read = AsyncMock(side_effect=[ServiceUnavailable("down"), _ABSENT])  # type: ignore[method-assign]
    await _outage_read(driver)
    assert driver.circuit_breaker.state is BreakerState.OPEN
    # recovery_timeout=0: the next call is a HALF_OPEN probe. The server answered
    # "absent", so the probe succeeded and its slot is released by closing.
    await _absent_read(driver)
    assert driver.circuit_breaker.state is BreakerState.CLOSED
    driver._raw_execute_read = AsyncMock(return_value=[{"ok": 1}])  # type: ignore[method-assign]
    assert await driver.execute_read("RETURN 1", {}, database="tenant-b") == [{"ok": 1}]
    assert driver.circuit_breaker.get_metrics().consecutive_failures == 0


# ── Expected write answers are breaker-neutral (GraphDriver.execute_write) ─────


async def test_constraint_conflicts_and_rolled_back_writes_never_count_against_the_breaker() -> None:
    driver = _breaker_driver(threshold=2)
    answers = [ConstraintError("duplicate evidence_ref"), ExpectedWriteOutcome("rolled back"), ConstraintError("dup")]
    driver._raw_execute_write = AsyncMock(side_effect=answers)  # type: ignore[method-assign]
    for answer in answers:
        with pytest.raises(type(answer)):
            await driver.execute_write(AsyncMock(), database="tenant-a")
    metrics = driver.circuit_breaker.get_metrics()
    assert (metrics.state, metrics.total_failures, metrics.consecutive_failures) == ("CLOSED", 0, 0)


async def test_a_real_write_outage_still_trips_the_breaker() -> None:
    driver = _breaker_driver(threshold=2)
    driver._raw_execute_write = AsyncMock(side_effect=ServiceUnavailable("down"))  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(ServiceUnavailable):
            await driver.execute_write(AsyncMock(), database="tenant-a")
    assert driver.circuit_breaker.state is BreakerState.OPEN


async def test_declared_types_bound_the_read_and_never_become_values() -> None:
    driver = _driver([])
    binding = GraphEvidenceBinding(driver, {}, relationship_types=("SYNTHETIC_A", "SYNTHETIC_B"))
    await binding.read(database="tenant-a", entity_refs=["a:1"])
    cypher, params = driver.execute_read.await_args.args
    assert "MATCH ()-[r:SYNTHETIC_A|SYNTHETIC_B]->()" in cypher
    assert "MATCH ()-[r]->()" not in cypher
    assert "SYNTHETIC_A" not in repr(params)


async def test_untyped_scan_is_kept_only_without_a_declaration() -> None:
    driver = _driver([])
    await GraphEvidenceBinding(driver, {}).read(database="tenant-a", entity_refs=["a:1"])
    assert "MATCH ()-[r]->()" in driver.execute_read.await_args.args[0]


async def test_an_empty_declaration_reads_nothing() -> None:
    driver = _driver([])
    read = await GraphEvidenceBinding(driver, {}, relationship_types=()).read(database="tenant-a", entity_refs=["a:1"])
    assert read == EvidenceRead(connected=True, database="tenant-a", observations=())
    driver.execute_read.assert_not_awaited()


@pytest.mark.parametrize("bad", [("R]->() DETACH DELETE r //",), ("BAD-TYPE",), ("",)])
def test_unsafe_relationship_types_are_refused_before_any_read(bad: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="Invalid label"):
        GraphEvidenceBinding(_driver([]), {}, relationship_types=bad)


def test_one_string_is_not_a_type_sequence() -> None:
    with pytest.raises(TypeError):
        GraphEvidenceBinding(_driver([]), {}, relationship_types="SYNTHETIC_A")


def test_declared_evidence_types_come_from_targetedge_endpoints_only() -> None:
    def endpoint(targetedge: str | None) -> MagicMock:
        return MagicMock(targetedge=targetedge)

    spec = MagicMock()
    spec.sync.endpoints = [endpoint("SYNTHETIC_B"), endpoint(None), endpoint("SYNTHETIC_A"), endpoint("SYNTHETIC_B")]
    assert declared_evidence_types(spec) == ("SYNTHETIC_A", "SYNTHETIC_B")
    spec.sync = None
    assert declared_evidence_types(spec) == ()
    spec.sync = MagicMock(endpoints=[endpoint("BAD-TYPE")])
    with pytest.raises(ValueError, match="Invalid label"):
        declared_evidence_types(spec)


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


@pytest.mark.usefixtures("_clean_state")
async def test_handle_score_is_blocked_during_a_graph_outage() -> None:
    binding = GraphEvidenceBinding(_driver(exc=ServiceUnavailable("connection refused")), approved_relevance_map({}))
    result = await handle_score("tenant-a", {"entity_refs": ["a:1"], "top_n": 1}, binding=binding)
    assert result["status"] == "blocked"
    assert result["evidence_reader"]["connected"] is False


@pytest.mark.parametrize(("enabled", "scored"), [(True, 1), (False, 0)], ids=["per-type", "ref-only"])
@pytest.mark.usefixtures("_clean_state")
async def test_handle_score_evidence_identity_follows_the_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, scored: int
) -> None:
    """A1: one evidence_ref under two relationship types abstains only under ref-only identity."""
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "evidence_identity_per_type_enabled", enabled)

    def edge(relationship_type: str) -> dict[str, Any]:
        props = {"evidence_ref": "e:1", "source_ref": "s", "confidence": 0.5}
        props |= {"assertion_status": "observed", "polarity": "positive"}
        return {"entity_ref": "a:1", "relationship_type": relationship_type, "props": props}

    binding = GraphEvidenceBinding(_driver([edge("R_ONE"), edge("R_TWO")]), approved_relevance_map({"R_ONE": 1.0}))
    result = await handle_score("tenant-a", {"entity_refs": ["a:1"], "top_n": 1}, binding=binding)
    assert len(result["scored"]) == scored
    assert [a["detail"] for a in result["abstained"]] == ([] if enabled else ["conflicting_evidence"])
