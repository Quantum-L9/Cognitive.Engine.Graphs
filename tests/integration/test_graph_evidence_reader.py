"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, evidence, graph, reader, tenancy, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_graph_evidence_reader.py
QM.POC0.CEG.GRAPH_EVIDENCE.002A read side against a real Neo4j: the local
``handle_score`` reads relationship evidence through ``GraphEvidenceBinding``
(``GraphDriver.execute_read``, READ transaction, no provisioning).

Evidence edges are seeded here in the exact shape relationship sync stamps
(``_evidence_schema``, ``_tenant``, ``_subject_ref``); the sync path that writes
them, and the end-to-end materialise-then-score witness, are
tests/integration/test_graph_evidence_score.py in the stacked write-side change.
Labels and relationship types are synthetic.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from engine.graph.driver import GraphDriver
from engine.handlers import handle_score
from engine.scoring.evidence_priority import approved_relevance_map
from engine.scoring.graph_evidence import GraphEvidenceBinding
from engine.state import get_state
from engine.sync.generator import RELATIONSHIP_EVIDENCE_SCHEMA

pytestmark = pytest.mark.integration

TENANT = "it-evidence-reader"
OTHER_TENANT = "it-evidence-other"
ABSENT_TENANT = "it-absent-tenant"
RELATION = "SYNTHETIC_TENDS"
SUBJECTS = ["synthetic.subject:1", "synthetic.subject:2"]

_SEED = """
CREATE (a:SyntheticSubject {subject_id: $s1, _tenant: $tenant})
CREATE (b:SyntheticSubject {subject_id: $s2, _tenant: $tenant})
CREATE (o:SyntheticObject {object_id: 'synthetic.object:9', _tenant: $tenant})
CREATE (a)-[:SYNTHETIC_TENDS {evidence_ref: 'evidence:1', source_ref: 'source:1', confidence: 0.8,
        assertion_status: 'observed', polarity: 'positive', source_revision: 'rev-1',
        _tenant: $tenant, _evidence_schema: $schema, _subject_ref: $s1, _object_ref: 'synthetic.object:9'}]->(o)
// Unstamped: a link that merely exists is never evidence.
CREATE (b)-[:SYNTHETIC_TENDS {evidence_ref: 'evidence:raw', source_ref: 'source:2', confidence: 1.0,
        assertion_status: 'observed', polarity: 'positive'}]->(o)
// Stamped for another tenant: never read for this one.
CREATE (b)-[:SYNTHETIC_TENDS {evidence_ref: 'evidence:foreign', source_ref: 'source:3', confidence: 1.0,
        assertion_status: 'observed', polarity: 'positive', _tenant: $other, _evidence_schema: $schema,
        _subject_ref: $s2, _object_ref: 'synthetic.object:9'}]->(o)
"""


async def _databases(driver: GraphDriver) -> set[str]:
    rows = await driver.execute_query("SHOW DATABASES YIELD name RETURN name", database="system")
    return {row["name"] for row in rows}


@pytest_asyncio.fixture
async def graph(graph_driver: GraphDriver) -> AsyncGenerator[GraphDriver, None]:
    await graph_driver.execute_query(f"CREATE DATABASE `{TENANT}` IF NOT EXISTS WAIT", database="system")
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)
    await graph_driver.execute_write(
        cypher=_SEED,
        parameters={
            "s1": SUBJECTS[0],
            "s2": SUBJECTS[1],
            "tenant": TENANT,
            "other": OTHER_TENANT,
            "schema": RELATIONSHIP_EVIDENCE_SCHEMA,
        },
        database=TENANT,
    )
    get_state().reset()
    yield graph_driver
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)
    get_state().reset()


def _binding(driver: GraphDriver, relevance: dict[str, float] | None = None) -> GraphEvidenceBinding:
    return GraphEvidenceBinding(driver, approved_relevance_map({RELATION: 1.0} if relevance is None else relevance))


async def _score(driver: GraphDriver, **binding_kwargs: Any) -> dict[str, Any]:
    return await handle_score(
        TENANT, {"entity_refs": SUBJECTS, "top_n": len(SUBJECTS)}, binding=_binding(driver, **binding_kwargs)
    )


async def test_local_score_reads_only_stamped_tenant_evidence(graph: GraphDriver) -> None:
    result = await _score(graph)
    assert result["status"] == "ok"
    assert result["evidence_reader"] == {"connected": True, "database": TENANT}
    assert result["source_revision"] == "rev-1"
    [scored] = result["scored"]
    assert scored["entity_ref"] == SUBJECTS[0]
    assert scored["score"] == 80.0
    assert scored["factors"][0]["evidence_refs"] == ["evidence:1"]
    # Subject 2 has an unstamped edge and an edge stamped for another tenant: neither is evidence.
    assert [a["entity_ref"] for a in result["abstained"]] == [SUBJECTS[1]]
    assert "evidence:raw" not in repr(result)
    assert "evidence:foreign" not in repr(result)


async def test_removing_the_edge_abstains_and_unapproved_types_never_qualify(graph: GraphDriver) -> None:
    unapproved = await _score(graph, relevance={"SOME_OTHER_REL": 1.0})
    assert unapproved["evidence_reader"]["connected"] is True
    assert unapproved["scored"] == []

    await graph.execute_write(
        cypher="MATCH ()-[r {evidence_ref: 'evidence:1'}]->() DELETE r", parameters={}, database=TENANT
    )
    removed = await _score(graph)
    assert removed["evidence_reader"]["connected"] is True
    assert removed["scored"] == []
    assert removed["source_revision"] is None


async def test_score_read_on_a_missing_database_is_blocked_and_never_creates_it(
    graph: GraphDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "auto_create_domain_database", True)
    assert ABSENT_TENANT not in await _databases(graph)
    result = await handle_score(ABSENT_TENANT, {"entity_refs": ["x.y:1"], "top_n": 1}, binding=_binding(graph))
    assert result["status"] == "blocked"
    assert result["evidence_reader"]["connected"] is False
    assert ABSENT_TENANT in result["evidence_reader"]["reason"]
    assert ABSENT_TENANT not in await _databases(graph)


async def test_execute_read_refuses_writes(graph: GraphDriver) -> None:
    with pytest.raises(Exception, match="(?i)write|access mode|read"):
        await graph.execute_read("CREATE (n:ReadModeProbe) RETURN n", database=TENANT)
    rows = await graph.execute_query("MATCH (n:ReadModeProbe) RETURN count(n) AS c", database=TENANT)
    assert rows[0]["c"] == 0
