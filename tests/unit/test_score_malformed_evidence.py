"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, evidence, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_score_malformed_evidence.py
SCR-06: a malformed evidence edge is not absent evidence. The policy
(``score_cohort``) and the reader (``GraphEvidenceBinding``) without Neo4j; the
real-graph proof through ``handle_score_remote`` is
tests/integration/test_score_malformed_evidence.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.settings import settings
from engine.scoring.evidence_priority import (
    EvidenceObservation,
    EvidenceRead,
    MalformedEvidence,
    parse_score_request,
    score_cohort,
)
from engine.scoring.graph_evidence import GraphEvidenceBinding

pytestmark = pytest.mark.unit

A = "ledger.depot:1"
B = "ledger.depot:2"
EDGE = "STOCKS"
RELEVANCE = {EDGE: 1.0}


def _obs(entity: str, ref: str, *, edge: str = EDGE, confidence: float = 0.8) -> EvidenceObservation:
    return EvidenceObservation(
        entity_ref=entity,
        evidence_ref=ref,
        relationship_type=edge,
        source_ref="source:crm-note:42",
        confidence=confidence,
        admission="admitted",
        polarity="positive",
    )


def _bad(entity: str, ref: str | None, *, edge: str = EDGE) -> MalformedEvidence:
    return MalformedEvidence(entity_ref=entity, relationship_type=edge, evidence_ref=ref)


def _score(observations: list[EvidenceObservation], malformed: list[MalformedEvidence]) -> dict[str, Any]:
    read = EvidenceRead(
        connected=True,
        database="it-tenant",
        observations=tuple(observations),
        malformed=tuple(malformed),
    )
    request = parse_score_request({"entity_refs": [A, B], "top_n": 2})
    return score_cohort(request, read, RELEVANCE, identity_per_type=True)


def _by_ref(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["entity_ref"]: row for row in [*result["scored"], *result["abstained"]]}


# ── Policy ─────────────────────────────────────────────────────────────────────


def test_only_malformed_evidence_abstains_as_malformed_not_as_no_evidence() -> None:
    rows = _by_ref(_score([], [_bad(A, "evidence:1")]))
    assert rows[A]["score"] is None
    assert rows[A]["detail"] == "malformed_evidence"
    assert rows[A]["malformed_evidence_count"] == 1
    # B has nothing at all: that one is genuinely no_evidence.
    assert rows[B]["detail"] == "no_evidence"
    assert rows[B]["malformed_evidence_count"] == 0


def test_a_malformed_edge_sharing_a_valid_identity_abstains() -> None:
    rows = _by_ref(_score([_obs(A, "evidence:1")], [_bad(A, "evidence:1")]))
    assert rows[A]["score"] is None
    assert rows[A]["detail"] == "malformed_evidence"
    assert rows[A]["malformed_evidence_count"] == 1


def test_a_malformed_edge_with_no_readable_ref_may_share_any_identity_and_abstains() -> None:
    rows = _by_ref(_score([_obs(A, "evidence:1")], [_bad(A, None)]))
    assert rows[A]["detail"] == "malformed_evidence"


def test_a_malformed_edge_with_a_distinct_identity_is_counted_and_the_entity_still_scores() -> None:
    rows = _by_ref(_score([_obs(A, "evidence:1")], [_bad(A, "evidence:2")]))
    assert rows[A]["score"] == 80.0
    assert rows[A]["malformed_evidence_count"] == 1


def test_identity_is_per_type_so_the_same_ref_on_another_type_is_distinct() -> None:
    rows = _by_ref(_score([_obs(A, "evidence:1")], [_bad(A, "evidence:1", edge="TENDED_BY")]))
    assert rows[A]["score"] == 80.0
    assert rows[A]["malformed_evidence_count"] == 1


def test_a_read_with_no_malformed_edge_adds_no_count() -> None:
    read = EvidenceRead(connected=True, database="it-tenant", observations=(_obs(A, "evidence:1"),))
    request = parse_score_request({"entity_refs": [A], "top_n": 1})
    [row] = score_cohort(request, read, RELEVANCE, identity_per_type=True)["scored"]
    assert "malformed_evidence_count" not in row


# ── Reader ─────────────────────────────────────────────────────────────────────


def _row(entity: str, **props: Any) -> dict[str, Any]:
    base = {
        "evidence_ref": "evidence:1",
        "source_ref": "source:crm-note:42",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
    }
    base.update(props)
    return {"entity_ref": entity, "relationship_type": EDGE, "props": base}


def _binding(rows: list[dict[str, Any]]) -> GraphEvidenceBinding:
    driver = MagicMock()
    driver.get_driver = MagicMock(return_value=object())
    driver.execute_read = AsyncMock(return_value=rows)
    return GraphEvidenceBinding(driver, RELEVANCE, relationship_types=[EDGE])


@pytest.mark.asyncio
async def test_the_reader_reports_a_malformed_edge_with_its_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", True)
    read = await _binding([_row(A), _row(A, polarity="sideways"), _row(B, evidence_ref=7, confidence="high")]).read(
        database="it-tenant", entity_refs=[A, B]
    )
    assert [o.entity_ref for o in read.observations] == [A]
    assert read.malformed == (_bad(A, "evidence:1"), _bad(B, None))


@pytest.mark.asyncio
async def test_flag_off_the_reader_drops_malformed_edges_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", False)
    read = await _binding([_row(A, polarity="sideways")]).read(database="it-tenant", entity_refs=[A])
    assert read.malformed == ()
    request = parse_score_request({"entity_refs": [A], "top_n": 1})
    [row] = score_cohort(request, read, RELEVANCE, identity_per_type=True)["abstained"]
    assert row["detail"] == "no_evidence"  # the legacy defect: a malformed edge reads as absent
