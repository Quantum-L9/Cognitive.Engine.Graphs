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

tests/integration/test_score_malformed_evidence.py
SCR-06 on a real graph, through the Gate-routed ``handle_score_remote``: an
evidence edge another writer put in the tenant database with CEG's evidence
stamps but an unreadable body is not absent evidence. Its entity abstains with
``malformed_evidence`` (never ``no_evidence``, never a score) when that edge is
its only evidence or may share an identity with a valid one, and every entity
reports ``malformed_evidence_count``.

Valid evidence is written by ``handle_sync`` (relationship sync). The malformed
edges are raw Cypher on purpose: they model the foreign writer this defect is
about, which never goes through CEG's sync.
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
from engine.state import get_state
from engine.sync.generator import RELATIONSHIP_EVIDENCE_SCHEMA
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

TENANT = "it-malformed-score"
EDGE = "TENDED_BY"
SHARED = "creamery.cave:101"  # a valid edge and a foreign edge with no readable ref
ONLY_BAD = "creamery.cave:102"  # nothing but a foreign edge
DISTINCT = "creamery.cave:103"  # a valid edge and a foreign edge under another ref
NOTHING = "creamery.cave:104"  # no edge at all
CAVES = [SHARED, ONLY_BAD, DISTINCT, NOTHING]
AFFINEUR = "creamery.affineur:7"


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
    _write_pack(tmp_path)
    await graph_driver.execute_query(f"CREATE DATABASE `{TENANT}` IF NOT EXISTS WAIT", database="system")
    await _wipe(graph_driver)
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))
    await handle_sync(TENANT, {"entity_type": "aging-caves", "batch": [{"cave_id": ref} for ref in CAVES]})
    await handle_sync(TENANT, {"entity_type": "affineurs", "batch": [{"affineur_id": AFFINEUR}]})
    yield graph_driver
    await _wipe(graph_driver)
    get_state().reset()


def _valid(subject: str, evidence_ref: str) -> dict[str, Any]:
    return {
        "from_id": subject,
        "to_id": AFFINEUR,
        "evidence_ref": evidence_ref,
        "source_ref": "source:crm-note:42",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
        "source_revision": "rev-2026-11-26",
    }


async def _foreign_edge(driver: GraphDriver, subject: str, props: dict[str, Any]) -> None:
    """An edge stamped as CEG evidence by a writer that is not CEG's sync, with an unreadable body."""
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


async def _seed(driver: GraphDriver) -> None:
    result = await handle_sync(
        TENANT, {"entity_type": "tended-by", "batch": [_valid(SHARED, "evidence:1"), _valid(DISTINCT, "evidence:2")]}
    )
    assert result["created_count"] == 2
    body = {"source_ref": "source:foreign:1", "confidence": 0.9, "assertion_status": "observed"}
    # No evidence_ref: its identity is unknown, so it may be evidence:1.
    await _foreign_edge(driver, SHARED, {**body, "polarity": "positive", "confidence": "high"})
    await _foreign_edge(driver, ONLY_BAD, {**body, "evidence_ref": "evidence:9", "polarity": "sideways"})
    await _foreign_edge(driver, DISTINCT, {**body, "evidence_ref": "evidence:3", "polarity": "sideways"})


async def _score() -> dict[str, dict[str, Any]]:
    result = await handle_score_remote(TENANT, {"entity_refs": CAVES, "top_n": len(CAVES)})
    assert result["status"] == "ok"
    return {row["entity_ref"]: row for row in [*result["scored"], *result["abstained"]]}


async def test_a_malformed_edge_is_never_absent_evidence(graph: GraphDriver) -> None:
    await _seed(graph)
    rows = await _score()

    assert rows[SHARED]["score"] is None
    assert rows[SHARED]["detail"] == "malformed_evidence"
    assert rows[ONLY_BAD]["score"] is None
    assert rows[ONLY_BAD]["detail"] == "malformed_evidence"
    # A malformed edge under its own ref neither hides nor changes the valid one.
    assert rows[DISTINCT]["score"] == 80.0
    assert rows[DISTINCT]["qualifying_evidence_refs"] == ["evidence:2"]
    assert rows[NOTHING]["detail"] == "no_evidence"
    assert {ref: row["malformed_evidence_count"] for ref, row in rows.items()} == {
        SHARED: 1,
        ONLY_BAD: 1,
        DISTINCT: 1,
        NOTHING: 0,
    }


async def test_flag_off_the_same_graph_reads_the_malformed_edges_as_absent(
    graph: GraphDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(graph)
    monkeypatch.setattr(settings, "score_malformed_evidence_abstention_enabled", False)
    rows = await _score()

    # The defect the flag closes: the valid edge scores although a foreign edge
    # that may be the same evidence is unreadable, and the bad-only entity reads
    # as having no evidence at all.
    assert rows[SHARED]["score"] == 80.0
    assert rows[ONLY_BAD]["detail"] == "no_evidence"
    assert all("malformed_evidence_count" not in row for row in rows.values())
