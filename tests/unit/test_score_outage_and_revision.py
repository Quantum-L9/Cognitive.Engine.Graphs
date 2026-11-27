"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, evidence, graph, reader]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_score_outage_and_revision.py
CEG-A04 and CEG-A08 on the graph evidence read (engine/scoring/graph_evidence.py),
both under ``score_malformed_evidence_abstention_enabled`` (FEATURE_GATES §38a):

- A04: a transaction timeout the driver translated into the engine's
  ``ExecutionError`` (neo4j error as ``__cause__``) is an outage, so the score is
  ``blocked``. Only that wrapper is unwrapped: an ``ExecutionError`` caused by bad
  Cypher, and raw syntax or authorization errors, still propagate.
- A08: a ``source_revision`` that is not a string (a foreign writer's list) makes
  the cohort's revision unknown instead of failing the read.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j.exceptions import AuthError, CypherSyntaxError, Neo4jError, ServiceUnavailable

from engine.config.settings import settings
from engine.handlers import ExecutionError, handle_score
from engine.scoring.evidence_priority import EvidenceUnavailableError, approved_relevance_map
from engine.scoring.graph_evidence import GraphEvidenceBinding, _single_revision
from engine.state import get_state

pytestmark = pytest.mark.unit

TIMEOUT_CODE = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"


def _neo4j(code: str, message: str) -> Neo4jError:
    return Neo4jError._hydrate_neo4j(code=code, message=message)


def _translated(cause: BaseException) -> ExecutionError:
    """What GraphDriver._translate_query_failure (#564) raises: ExecutionError from the neo4j error."""
    translated = ExecutionError("Neo4j terminated the query", action="graph_query", tenant="tenant-a")
    translated.__cause__ = cause
    return translated


def _driver(rows: list[dict[str, Any]] | None = None, exc: BaseException | None = None) -> MagicMock:
    driver = MagicMock()
    driver.execute_read = AsyncMock(return_value=rows or [], side_effect=exc)
    return driver


async def _read(driver: MagicMock) -> Any:
    return await GraphEvidenceBinding(driver, {}).read(database="tenant-a", entity_refs=["a:1"])


@pytest.fixture
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", True)


@pytest.fixture
def _clean_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


# ── A04: translated timeouts are outages ───────────────────────────────────────


@pytest.mark.usefixtures("_flag_on")
async def test_a_raw_transaction_timeout_is_an_outage() -> None:
    with pytest.raises(EvidenceUnavailableError, match="unavailable"):
        await _read(_driver(exc=_neo4j(TIMEOUT_CODE, "timed out")))


@pytest.mark.usefixtures("_flag_on")
async def test_a_translated_transaction_timeout_is_an_outage() -> None:
    with pytest.raises(EvidenceUnavailableError, match="unavailable"):
        await _read(_driver(exc=_translated(_neo4j(TIMEOUT_CODE, "timed out"))))


@pytest.mark.usefixtures("_flag_on")
async def test_a_translated_connection_error_is_an_outage_and_a_raw_one_is_unchanged() -> None:
    with pytest.raises(EvidenceUnavailableError):
        await _read(_driver(exc=ServiceUnavailable("connection refused")))
    with pytest.raises(EvidenceUnavailableError):
        await _read(_driver(exc=_translated(ServiceUnavailable("connection refused"))))


@pytest.mark.parametrize(
    "defect",
    [
        CypherSyntaxError("Invalid input"),
        _neo4j("Neo.ClientError.Security.Unauthorized", "unauthorized"),
        _translated(CypherSyntaxError("Invalid input")),
        _translated(_neo4j("Neo.ClientError.Security.Unauthorized", "unauthorized")),
        ExecutionError("no cause", action="graph_query", tenant="tenant-a"),
    ],
    ids=["syntax", "auth", "translated-syntax", "translated-auth", "execution-error-without-cause"],
)
@pytest.mark.usefixtures("_flag_on")
async def test_wrong_questions_still_propagate(defect: Exception) -> None:
    if isinstance(defect, Neo4jError) and not isinstance(defect, CypherSyntaxError):
        assert isinstance(defect, AuthError)
    with pytest.raises(type(defect)):
        await _read(_driver(exc=defect))


async def test_flag_off_a_translated_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", False)
    with pytest.raises(ExecutionError):
        await _read(_driver(exc=_translated(_neo4j(TIMEOUT_CODE, "timed out"))))


@pytest.mark.usefixtures("_flag_on", "_clean_state")
async def test_handle_score_is_blocked_on_a_translated_timeout() -> None:
    driver = _driver(exc=_translated(_neo4j(TIMEOUT_CODE, "timed out")))
    binding = GraphEvidenceBinding(driver, approved_relevance_map({}))
    result = await handle_score("tenant-a", {"entity_refs": ["a:1"], "top_n": 1}, binding=binding)
    assert result["status"] == "blocked"
    assert result["evidence_reader"]["connected"] is False
    assert result["scored"] == []


# ── A08: a malformed source_revision is unknown, never a failure ───────────────


def _rows(*revisions: Any) -> list[dict[str, Any]]:
    return [{"props": {} if r is _ABSENT else {"source_revision": r}} for r in revisions]


_ABSENT = object()


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        ((["r1", "r2"],), None),
        ((None,), None),
        (("r1",), "r1"),
        (("r1", "r1"), "r1"),
        (("r1", ["r1"]), None),
        (("r1", None), None),
        (("r1", 7), None),
        ((["r1"], ["r1"]), None),
        ((_ABSENT, "r1"), None),
    ],
    ids=["list", "null", "str", "same-str", "str-and-list", "str-and-null", "str-and-int", "only-lists", "absent"],
)
@pytest.mark.usefixtures("_flag_on")
def test_a_revision_that_is_not_a_string_is_unknown(revisions: tuple[Any, ...], expected: str | None) -> None:
    assert _single_revision(_rows(*revisions)) == expected


def _edge(ref: str, **props: Any) -> dict[str, Any]:
    base = {"evidence_ref": ref, "source_ref": "s", "confidence": 0.5}
    base |= {"assertion_status": "observed", "polarity": "positive"}
    return {"entity_ref": "a:1", "relationship_type": "R", "props": {**base, **props}}


@pytest.mark.usefixtures("_flag_on")
async def test_valid_evidence_with_malformed_revision_metadata_still_reads() -> None:
    read = await _read(_driver([_edge("e:1", source_revision=["r1", "r2"]), _edge("e:2", source_revision="r1")]))
    assert [o.evidence_ref for o in read.observations] == ["e:1", "e:2"]
    assert read.source_revision is None


@pytest.mark.usefixtures("_flag_on")
async def test_only_malformed_evidence_with_a_list_revision_still_reads() -> None:
    read = await _read(_driver([_edge("e:1", polarity="sideways", source_revision=["r1"])]))
    assert read.observations == ()
    assert len(read.malformed) == 1
    assert read.source_revision is None


def test_flag_off_the_legacy_revision_comparison_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", False)
    assert _single_revision(_rows("r1", "r1")) == "r1"
    with pytest.raises(TypeError, match="unhashable"):
        _single_revision(_rows(["r1", "r2"]))
