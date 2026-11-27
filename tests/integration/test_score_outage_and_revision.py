"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, evidence, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_score_outage_and_revision.py
CEG-A04 and CEG-A08 on a real graph, through the Gate-routed ``handle_score_remote``
(FEATURE_GATES §38a, ``score_malformed_evidence_abstention_enabled``):

- A04: a read the server terminates at its transaction timeout answers
  ``blocked``, never an error. Requires #564: its driver gives every engine
  query the client transaction timeout ``neo4j_query_timeout_s`` (default 25 s),
  which takes precedence over the server's ``db.transaction.timeout``, and
  translates the timeout into ``ExecutionError``. The test pins that timeout to
  1 s on the settings the registered driver reads per call. Without #564 there is
  no client timeout, so the server's ``db.transaction.timeout`` (dynamic in
  Neo4j 5) is also set to 1 s and restored after. The read is a statement that
  cannot finish, on the real driver in place of the evidence query, so the
  timeout comes from Neo4j itself.
- A08: an evidence edge a foreign writer stored with ``source_revision`` as a
  list does not fail the cohort read; the revision is reported unknown.

Valid evidence is written by ``handle_sync``. The foreign edge is raw Cypher on
purpose: it models the writer this defect is about, which never goes through
CEG's sync.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import handle_score_remote, handle_sync, init_dependencies
from engine.scoring import graph_evidence
from engine.state import get_state
from engine.sync.generator import RELATIONSHIP_EVIDENCE_SCHEMA
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

TENANT = "it-score-outage-revision"
EDGE = "TENDED_BY"
CAVE = "creamery.cave:201"
OTHER = "creamery.cave:202"
AFFINEUR = "creamery.affineur:8"
TIMEOUT_S = 1.0
TIMEOUT = "1s"
# 4 * 10^18 rows: no server completes it, so only the transaction timeout ends it.
SLOW_CYPHER = (
    "UNWIND range(1, 2000000000) AS i UNWIND range(1, 2000000000) AS j "
    "WITH i + j AS k WHERE k % 7 = 3 RETURN count(k) AS c"
)


def _write_pack(root: Path) -> None:
    spec = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text())
    spec["domain"]["id"] = TENANT
    spec["domain"]["version"] = "1.0.0-reviewed"  # synthetic approval: relevance is honoured
    for edge in spec["ontology"]["edges"]:
        if edge["type"] == EDGE:
            edge["evidencerelevance"] = 1.0
    spec["sync"]["endpoints"] += [
        {"path": "/v1/sync/affineurs", "method": "POST", "batchstrategy": "UNWINDMERGE"}
        | {"targetnode": "Affineur", "idproperty": "affineur_id"},
        {"path": "/v1/sync/tended-by", "method": "POST", "batchstrategy": "UNWINDMERGE"}
        | {"targetedge": EDGE, "fromnode": "AgingCave", "tonode": "Affineur"},
    ]
    spec["capabilities"] = [{"name": "score_read", "actions": ["score:read"], "allowed_subjects": [TENANT]}]
    (root / TENANT).mkdir()
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))


async def _wipe(driver: GraphDriver) -> None:
    await driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)


@pytest_asyncio.fixture
async def graph(
    graph_driver: GraphDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[GraphDriver, None]:
    monkeypatch.setattr(settings, "relationship_sync_enabled", True)
    monkeypatch.setattr(settings, "remote_score_enabled", True)
    monkeypatch.setattr(settings, "evidence_identity_per_type_enabled", True)
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", True)
    _write_pack(tmp_path)
    await graph_driver.execute_query(f"CREATE DATABASE `{TENANT}` IF NOT EXISTS WAIT", database="system")
    await _wipe(graph_driver)
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))
    await handle_sync(TENANT, {"entity_type": "aging-caves", "batch": [{"cave_id": c} for c in (CAVE, OTHER)]})
    await handle_sync(TENANT, {"entity_type": "affineurs", "batch": [{"affineur_id": AFFINEUR}]})
    yield graph_driver
    await _wipe(graph_driver)
    get_state().reset()


@pytest_asyncio.fixture
async def server_timeout(graph: GraphDriver, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[GraphDriver, None]:
    """Every read the handler issues runs under a 1 s transaction timeout.

    #564's client timeout (read from ``settings`` on each call by the driver the
    handler is wired with) is pinned to 1 s, since it overrides the server's
    default. The server's ``db.transaction.timeout`` is set to 1 s as well, for a
    driver without a client timeout, and restored after.
    """
    if "neo4j_query_timeout_s" in type(settings).model_fields:
        monkeypatch.setattr(settings, "neo4j_query_timeout_enabled", True)
        monkeypatch.setattr(settings, "neo4j_query_timeout_s", TIMEOUT_S)
    rows = await graph.execute_query(
        "SHOW SETTINGS YIELD name, value WHERE name = 'db.transaction.timeout' RETURN value", database="system"
    )
    original = rows[0]["value"] if rows else "0s"
    await graph.execute_query(
        "CALL dbms.setConfigValue('db.transaction.timeout', $v)", {"v": TIMEOUT}, database="system"
    )
    try:
        yield graph
    finally:
        await graph.execute_query(
            "CALL dbms.setConfigValue('db.transaction.timeout', $v)", {"v": original}, database="system"
        )


def _valid(subject: str, evidence_ref: str, revision: str) -> dict[str, Any]:
    return {
        "from_id": subject,
        "to_id": AFFINEUR,
        "evidence_ref": evidence_ref,
        "source_ref": "source:crm-note:42",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
        "source_revision": revision,
    }


async def _foreign_edge(driver: GraphDriver, subject: str, props: dict[str, Any]) -> None:
    """An edge stamped as CEG evidence by a writer that is not CEG's sync."""
    await driver.execute_write(
        cypher=(
            f"MATCH (a:AgingCave {{cave_id: $subject}}), (b:Affineur {{affineur_id: $affineur}}) "
            f"CREATE (a)-[r:{EDGE}]->(b) SET r = $props, r._tenant = $tenant, "
            "r._evidence_schema = $schema, r._subject_ref = $subject, r._object_ref = $affineur"
        ),
        parameters={
            "subject": subject,
            "affineur": AFFINEUR,
            "props": props,
            "tenant": TENANT,
            "schema": RELATIONSHIP_EVIDENCE_SCHEMA,
        },
        database=TENANT,
    )


async def test_a_read_the_server_times_out_is_blocked_not_an_error(
    server_timeout: GraphDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await handle_sync(TENANT, {"entity_type": "tended-by", "batch": [_valid(CAVE, "evidence:1", "r1")]})
    assert result["created_count"] == 1
    # The binding's read becomes a statement the server must terminate.
    monkeypatch.setattr(graph_evidence, "read_evidence_query", lambda _types: SLOW_CYPHER)

    scored = await handle_score_remote(TENANT, {"entity_refs": [CAVE], "top_n": 1})

    assert scored["status"] == "blocked"
    assert scored["evidence_reader"]["connected"] is False
    assert scored["scored"] == []


async def test_a_list_source_revision_from_a_foreign_writer_does_not_fail_the_cohort(graph: GraphDriver) -> None:
    result = await handle_sync(TENANT, {"entity_type": "tended-by", "batch": [_valid(CAVE, "evidence:1", "r1")]})
    assert result["created_count"] == 1
    body = {"source_ref": "source:foreign:1", "confidence": 0.9, "assertion_status": "observed"}
    await _foreign_edge(
        graph, OTHER, {**body, "evidence_ref": "evidence:2", "polarity": "positive"} | {"source_revision": ["r1", "r2"]}
    )

    scored = await handle_score_remote(TENANT, {"entity_refs": [CAVE, OTHER], "top_n": 2})

    assert scored["status"] == "ok"
    answered = [*scored["scored"], *scored["abstained"]]
    assert {row["entity_ref"] for row in answered} == {CAVE, OTHER}
    # The revision is metadata, not evidence: neither entity reads as malformed for it.
    assert all(row.get("detail") != "malformed_evidence" for row in scored["abstained"])
    assert scored["source_revision"] is None
