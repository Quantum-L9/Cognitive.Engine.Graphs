"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, relationship, evidence, scoring, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_relationship_sync.py
Unit tests for QM.POC0.CEG.GRAPH_EVIDENCE.002A without Neo4j: relationship sync
declaration resolution and row admission (engine/sync/generator.py), the
graph evidence binding's failure modes (engine/scoring/graph_evidence.py) and the
handler seams. The real-graph proof is tests/integration/test_graph_evidence_score.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from engine.config.schema import DomainSpec, SyncEndpointSpec
from engine.graph.driver import DatabaseNotProvisionedError
from engine.handlers import ValidationError, handle_score, handle_sync, init_dependencies
from engine.scoring.evidence_priority import (
    EvidenceRead,
    EvidenceUnavailableError,
    approved_relevance_map,
)
from engine.scoring.graph_evidence import MAX_EVIDENCE_EDGES, GraphEvidenceBinding, _is_stale, _single_revision
from engine.state import get_state
from engine.sync.generator import (
    MAX_RELATIONSHIP_BATCH,
    normalize_relationship_rows,
    relationship_merge_query,
    relationship_precheck_query,
    resolve_relationship_declaration,
)
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

EDGE_ENDPOINTS = {
    "cheese-aging": [
        {"path": "/v1/sync/affineurs", "targetnode": "Affineur", "idproperty": "affineur_id"},
        {"path": "/v1/sync/tended-by", "targetedge": "TENDED_BY", "fromnode": "AgingCave", "tonode": "Affineur"},
    ],
    "orbital-logistics": [
        {"path": "/v1/sync/stocks", "targetedge": "STOCKS", "fromnode": "OrbitalDepot", "tonode": "PropellantGrade"},
    ],
}


def _spec(domain_id: str, extra: list[dict[str, Any]] | None = None) -> DomainSpec:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / domain_id / "spec.yaml").read_text())
    for endpoint in EDGE_ENDPOINTS[domain_id] + (extra or []):
        raw["sync"]["endpoints"].append({"method": "POST", "batchstrategy": "UNWINDMERGE", **endpoint})
    return DomainSpec(**raw)


def _endpoint(spec: DomainSpec, path: str) -> SyncEndpointSpec:
    return next(e for e in spec.sync.endpoints if e.path == path)  # type: ignore[union-attr]


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "from_id": "creamery.cave:101",
        "to_id": "creamery.affineur:7",
        "evidence_ref": "evidence:1",
        "source_ref": "source:1",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
    }
    row.update(overrides)
    return row


# ── Declaration resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("domain_id", "path", "expected"),
    [
        ("cheese-aging", "/v1/sync/tended-by", ("TENDED_BY", "AgingCave", "cave_id", "Affineur", "affineur_id")),
        (
            "orbital-logistics",
            "/v1/sync/stocks",
            ("STOCKS", "OrbitalDepot", "depot_id", "PropellantGrade", "grade_code"),
        ),
    ],
)
def test_declaration_resolves_from_the_tenant_ontology(domain_id: str, path: str, expected: tuple[str, ...]) -> None:
    spec = _spec(domain_id)
    d = resolve_relationship_declaration(spec, _endpoint(spec, path))
    assert (d.edge_type, d.from_label, d.from_id_property, d.to_label, d.to_id_property) == expected


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        (
            {"path": "/v1/sync/x", "targetedge": "UNDECLARED", "fromnode": "AgingCave", "tonode": "Affineur"},
            "exactly one edge",
        ),
        (
            {"path": "/v1/sync/x", "targetedge": "TENDED_BY", "fromnode": "Affineur", "tonode": "AgingCave"},
            "exactly one edge",
        ),
        (
            {"path": "/v1/sync/x", "targetedge": "SHARES_CULTURE_WITH", "fromnode": "AgingCave", "tonode": "AgingCave"},
            "DIRECTED and managedby sync",
        ),
        ({"path": "/v1/sync/x", "targetedge": "TENDED_BY", "fromnode": "AgingCave"}, "targetedge, fromnode and tonode"),
        (
            {
                "path": "/v1/sync/x",
                "targetedge": "TENDED_BY",
                "fromnode": "AgingCave",
                "tonode": "Affineur",
                "targetnode": "AgingCave",
            },
            "both targetnode and targetedge",
        ),
        (
            {
                "path": "/v1/sync/x",
                "targetedge": "TENDED_BY",
                "fromnode": "AgingCave",
                "tonode": "Affineur",
                "batchstrategy": "UNWINDMATCHSET",
            },
            "UNWINDMERGE",
        ),
    ],
)
def test_undeclared_misdirected_or_ambiguous_endpoints_are_refused(endpoint: dict[str, Any], match: str) -> None:
    spec = _spec("cheese-aging", [endpoint])
    with pytest.raises(ValueError, match=match):
        resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/x"))


def test_conflicting_node_id_declarations_are_refused() -> None:
    spec = _spec("cheese-aging", [{"path": "/v1/sync/affineurs-2", "targetnode": "Affineur", "idproperty": "other_id"}])
    with pytest.raises(ValueError, match="conflicting sync idproperty"):
        resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/tended-by"))


# ── Row admission ──────────────────────────────────────────────────────────────


def test_valid_rows_normalise_to_write_shape() -> None:
    [row] = normalize_relationship_rows([_row(confidence=1, source_revision="rev-1")])
    assert row == {
        "from_id": "creamery.cave:101",
        "to_id": "creamery.affineur:7",
        "evidence_ref": "evidence:1",
        "props": {
            "evidence_ref": "evidence:1",
            "source_ref": "source:1",
            "confidence": 1.0,
            "assertion_status": "observed",
            "polarity": "positive",
            "source_revision": "rev-1",
        },
    }
    assert isinstance(row["props"]["confidence"], float)


def test_identical_rows_deduplicate_and_differing_reuse_is_refused() -> None:
    assert len(normalize_relationship_rows([_row(), _row()])) == 1
    with pytest.raises(ValueError, match="repeated with different content"):
        normalize_relationship_rows([_row(), _row(confidence=0.3)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_ref": ""},
        {"evidence_ref": None},
        {"source_ref": "  "},
        {"confidence": None},
        {"confidence": -0.1},
        {"confidence": 1.01},
        {"confidence": float("nan")},
        {"confidence": True},
        {"confidence": "0.8"},
        {"assertion_status": "rumoured"},
        {"assertion_status": None},
        {"polarity": "neutral"},
        {"from_id": True},
        {"from_id": 1.5},
        {"to_id": ""},
        {"expires_at": "2026-01-01T00:00:00"},
        {"expires_at": "not a date"},
        {"label": "Other"},
        {"relationship_type": "OTHER"},
        {"tenant": "someone-else"},
        {"database": "neo4j"},
    ],
)
def test_invalid_rows_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        normalize_relationship_rows([_row(**overrides)])


@pytest.mark.parametrize(
    "missing", ["from_id", "to_id", "evidence_ref", "source_ref", "confidence", "assertion_status", "polarity"]
)
def test_rows_missing_a_required_field_are_refused(missing: str) -> None:
    row = _row()
    del row[missing]
    with pytest.raises(ValueError, match="missing"):
        normalize_relationship_rows([row])


def test_batch_size_is_bounded() -> None:
    with pytest.raises(ValueError):
        normalize_relationship_rows([])
    rows = [_row(evidence_ref=f"e:{i}") for i in range(MAX_RELATIONSHIP_BATCH + 1)]
    with pytest.raises(ValueError):
        normalize_relationship_rows(rows)


def test_queries_take_structure_from_the_declaration_and_values_only_as_parameters() -> None:
    spec = _spec("orbital-logistics")
    d = resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/stocks"))
    for cypher in (relationship_precheck_query(d), relationship_merge_query(d)):
        assert "OrbitalDepot" in cypher
        assert "PropellantGrade" in cypher
        assert ":STOCKS" in cypher
        assert "$batch" in cypher
        assert "$tenant" in cypher
    merge = relationship_merge_query(d)
    assert "ON CREATE SET" in merge
    assert "ON MATCH" not in merge


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


@pytest.mark.usefixtures("_clean_state")
async def test_refused_relationship_batch_never_reaches_the_graph() -> None:
    spec = _spec("cheese-aging")
    loader = MagicMock()
    loader.load_domain.return_value = spec
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    init_dependencies(driver, loader)
    with pytest.raises(ValidationError, match="unsupported keys"):
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row(cypher="MATCH (n) DELETE n")]})
    driver.execute_query.assert_not_awaited()


@pytest.mark.usefixtures("_clean_state")
async def test_missing_endpoint_refuses_the_whole_batch_before_any_write() -> None:
    spec = _spec("cheese-aging")
    loader = MagicMock()
    loader.load_domain.return_value = spec
    precheck = [
        {"evidence_ref": "evidence:1", "from_count": 1, "from_any": 1, "to_count": 1, "to_any": 1, "existing": []},
        {"evidence_ref": "evidence:2", "from_count": 1, "from_any": 1, "to_count": 0, "to_any": 0, "existing": []},
    ]
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=precheck)
    init_dependencies(driver, loader)
    with pytest.raises(ValidationError, match="nothing written"):
        await handle_sync(
            "cheese-aging", {"entity_type": "tended-by", "batch": [_row(), _row(evidence_ref="evidence:2")]}
        )
    assert driver.execute_query.await_count == 1  # the precheck only; no MERGE
