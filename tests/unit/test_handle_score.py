"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, scoring, tenancy, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_handle_score.py
Unit tests for the local, unadvertised ``engine.handlers.handle_score``.

The bindings here are synthetic, in-memory and report ``connected=False``. They
exercise the handler's validation, tenant confinement and composition only.
The real Neo4j tenant-isolation witness (acceptance 6, 7, 10) is NOT RUN: no
admitted EIE -> Gate -> CEG projection with relationship evidence exists yet.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine import handlers
from engine.handlers import ACTION_HANDLERS, ValidationError, handle_score
from engine.scoring.evidence_priority import (
    EvidenceObservation,
    EvidenceRead,
    approved_relevance_map,
)
from engine.state import get_state

ROOT = Path(__file__).resolve().parents[2]
REL = "SYNTHETIC_POSITIVE_REL"
A_REF = "hubspot.company:101"
B_REF = "hubspot.company:201"


def _observation(entity_ref: str, evidence_ref: str, confidence: float) -> EvidenceObservation:
    return EvidenceObservation(
        entity_ref=entity_ref,
        evidence_ref=evidence_ref,
        relationship_type=REL,
        source_ref=f"source:{evidence_ref}",
        confidence=confidence,
        admission="admitted",
        polarity="positive",
    )


class TwoTenantBinding:
    """In-memory evidence keyed by database; records every read."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, tuple[str, ...]]] = []
        self._graphs = {
            "tenant-a": (_observation(A_REF, "evidence:a1", 0.8),),
            "tenant-b": (_observation(B_REF, "evidence:b1", 0.9),),
        }

    @property
    def approved_relevance(self) -> Mapping[str, float]:
        return approved_relevance_map({REL: 1.0})

    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        self.reads.append((database, tuple(entity_refs)))
        observations = tuple(o for o in self._graphs.get(database, ()) if o.entity_ref in entity_refs)
        return EvidenceRead(connected=False, database=database, observations=observations)


class WrongDatabaseBinding(TwoTenantBinding):
    async def read(self, *, database: str, entity_refs: Sequence[str]) -> EvidenceRead:
        return EvidenceRead(connected=False, database="tenant-b", observations=())


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


async def test_without_binding_result_is_blocked_and_nothing_is_scored() -> None:
    result = await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1})
    assert result["status"] == "blocked"
    assert result["evidence_reader"]["connected"] is False
    assert result["scored"] == []
    assert result["abstained"] == []
    assert result["source_revision"] is None


async def test_binding_reads_only_the_tenant_database() -> None:
    binding = TwoTenantBinding()
    result = await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=binding)
    assert binding.reads == [("tenant-a", (A_REF,))]
    assert result["scored"][0]["score"] == 80.0
    assert result["scored"][0]["factors"][0]["evidence_refs"] == ["evidence:a1"]


async def test_tenant_a_cannot_score_or_see_tenant_b_ref() -> None:
    binding = TwoTenantBinding()
    result = await handle_score("tenant-a", {"entity_refs": [A_REF, B_REF], "top_n": 2}, binding=binding)
    assert binding.reads == [("tenant-a", (A_REF, B_REF))]
    assert [e["entity_ref"] for e in result["scored"]] == [A_REF]
    assert result["abstained"][0]["entity_ref"] == B_REF
    assert result["abstained"][0]["score"] is None
    assert "evidence:b1" not in repr(result)


async def test_binding_that_reads_another_database_is_refused() -> None:
    with pytest.raises(handlers.ExecutionError, match="not the tenant database"):
        await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=WrongDatabaseBinding())


@pytest.mark.parametrize(
    "payload",
    [
        {"entity_refs": [A_REF], "top_n": 1, "tenant": "tenant-b"},
        {"entity_refs": [A_REF], "top_n": 1, "database": "tenant-b"},
        {"entity_refs": [A_REF], "top_n": 1, "weights": {REL: 1.0}},
        {"entity_refs": [A_REF], "top_n": 1, "cypher": "MATCH (n) DETACH DELETE n"},
        {"entity_refs": [A_REF, A_REF], "top_n": 1},
        {"entity_refs": [f"hubspot.company:{i}" for i in range(26)], "top_n": 1},
    ],
)
async def test_invalid_requests_are_refused_before_any_read(payload: dict[str, Any]) -> None:
    binding = TwoTenantBinding()
    with pytest.raises(ValidationError):
        await handle_score("tenant-a", payload, binding=binding)
    assert binding.reads == []


async def test_illegal_tenant_id_is_refused_before_any_read() -> None:
    binding = TwoTenantBinding()
    with pytest.raises(ValidationError, match="not a legal tenant id"):
        await handle_score("Tenant A", {"entity_refs": [A_REF], "top_n": 1}, binding=binding)
    assert binding.reads == []


async def test_tenant_outside_allowlist_is_refused_before_any_read() -> None:
    get_state()._tenant_allowlist = {"tenant-b"}
    binding = TwoTenantBinding()
    with pytest.raises(ValidationError, match="allowlist"):
        await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=binding)
    assert binding.reads == []


async def test_handler_works_with_match_path_symbols_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 8: nothing on the match path is on the score call graph."""

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        msg = "match-path symbol used by handle_score"
        raise AssertionError(msg)

    for symbol in (
        "DomainPackLoader",
        "GateCompiler",
        "TraversalAssembler",
        "ScoringAssembler",
        "handle_match",
        "_require_deps",
        "_enforce_capability",
    ):
        monkeypatch.setattr(handlers, symbol, unavailable)
    result = await handle_score("tenant-a", {"entity_refs": [A_REF], "top_n": 1}, binding=TwoTenantBinding())
    assert result["scored"][0]["entity_ref"] == A_REF


def test_score_is_neither_registered_nor_advertised() -> None:
    """Acceptance 9: no chassis route and no Gate advertisement in this slice."""
    assert "score" not in ACTION_HANDLERS
    spec = yaml.safe_load((ROOT / "engine" / "spec.yaml").read_text(encoding="utf-8"))
    assert spec["node"]["actions"] == ["match", "sync", "outcomes", "resolve", "health-assess"]
