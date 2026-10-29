"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, sync, relationship, evidence, identity, scoring, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_tenant_mapping_identity.py
QM.POC0.TENANT_ASSOCIATION_MAPPING.004 D-13 against a real Neo4j: when the tenant
spec declares ``refnamespace`` on its node endpoints, node sync, relationship sync
and scoring resolve one canonical identity (``<namespace>:<record id>``), and an
id outside the namespace is refused with nothing written.

The tenant is the synthetic ``cheese-aging`` verification domain under a new id;
the provider association in ``observedfrom`` is placeholder vocabulary.
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
from engine.handlers import ValidationError, handle_score, handle_sync, init_dependencies
from engine.scoring.evidence_priority import approved_relevance_map
from engine.scoring.graph_evidence import GraphEvidenceBinding
from engine.state import get_state
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

TENANT = "it-mapped-cheese"
CAVES = ["creamery.cave:101", "creamery.cave:102"]
AFFINEUR = "creamery.affineur:7"


@pytest.fixture(scope="module")
def domains_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("mapped-domains")
    spec = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text())
    spec["domain"].update({"id": TENANT, "version": "1.1.0-reviewed"})
    endpoints = spec["sync"]["endpoints"]
    endpoints[0]["refnamespace"] = "creamery.cave"
    endpoints += [
        {
            "path": "/v1/sync/affineurs",
            "method": "POST",
            "targetnode": "Affineur",
            "idproperty": "affineur_id",
            "batchstrategy": "UNWINDMERGE",
            "refnamespace": "creamery.affineur",
        },
        {
            "path": "/v1/sync/tended-by",
            "method": "POST",
            "targetedge": "TENDED_BY",
            "fromnode": "AgingCave",
            "tonode": "Affineur",
            "batchstrategy": "UNWINDMERGE",
            "observedfrom": [
                {
                    "provider": "synthcrm",
                    "fromobjecttype": "caves",
                    "toobjecttype": "affineurs",
                    "associationcategory": "SYNTH_DEFINED",
                    "associationtypeid": 7,
                }
            ],
        },
    ]
    (root / TENANT).mkdir()
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    return root


async def _wipe(driver: GraphDriver) -> None:
    await driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=TENANT)


async def _count(driver: GraphDriver, cypher: str) -> int:
    [row] = await driver.execute_query(cypher, database=TENANT)
    return int(row["n"])


@pytest_asyncio.fixture
async def graph(
    graph_driver: GraphDriver, domains_root: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[GraphDriver, DomainPackLoader], None]:
    monkeypatch.setattr(settings, "relationship_sync_enabled", True)
    await graph_driver.execute_query(f"CREATE DATABASE `{TENANT}` IF NOT EXISTS WAIT", database="system")
    await _wipe(graph_driver)
    get_state().reset()
    loader = DomainPackLoader(config_path=str(domains_root))
    init_dependencies(graph_driver, loader)
    yield graph_driver, loader
    await _wipe(graph_driver)
    get_state().reset()


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "from_id": CAVES[0],
        "to_id": AFFINEUR,
        "evidence_ref": "evidence:synthcrm:association:1",
        "source_ref": "synthcrm:caves/101->affineurs/7#SYNTH_DEFINED:7",
        "confidence": 1.0,
        "assertion_status": "observed",
        "polarity": "positive",
    }
    row.update(overrides)
    return row


async def test_one_identity_from_node_sync_through_relationship_sync_to_scoring(
    graph: tuple[GraphDriver, DomainPackLoader],
) -> None:
    driver, _ = graph
    await handle_sync(TENANT, {"entity_type": "aging-caves", "batch": [{"cave_id": ref} for ref in CAVES]})
    await handle_sync(TENANT, {"entity_type": "affineurs", "batch": [{"affineur_id": AFFINEUR}]})
    result = await handle_sync(TENANT, {"entity_type": "tended-by", "batch": [_row()]})
    assert result["created_count"] == 1

    [edge] = await driver.execute_query(
        "MATCH (a)-[r:TENDED_BY]->(b) RETURN a.cave_id AS a, b.affineur_id AS b, "
        "r._subject_ref AS subject, r._object_ref AS object",
        database=TENANT,
    )
    assert edge == {"a": CAVES[0], "b": AFFINEUR, "subject": CAVES[0], "object": AFFINEUR}

    binding = GraphEvidenceBinding(driver, approved_relevance_map({"TENDED_BY": 0.5}))
    scored = await handle_score(TENANT, {"entity_refs": CAVES, "top_n": 2}, binding=binding)
    assert [s["entity_ref"] for s in scored["scored"]] == [CAVES[0]]
    assert scored["scored"][0]["score"] == 50.0  # confidence 1.0 x relevance 0.5
    assert [a["entity_ref"] for a in scored["abstained"]] == [CAVES[1]]


@pytest.mark.parametrize("record_id", ["101", "creamery.affineur:101", "creamery.cave:"])
async def test_a_node_id_outside_the_namespace_is_refused_and_writes_nothing(
    graph: tuple[GraphDriver, DomainPackLoader], record_id: str
) -> None:
    driver, _ = graph
    with pytest.raises(ValidationError, match="is not a graph ref creamery.cave:<id>"):
        await handle_sync(
            TENANT, {"entity_type": "aging-caves", "batch": [{"cave_id": CAVES[0]}, {"cave_id": record_id}]}
        )
    assert await _count(driver, "MATCH (n) RETURN count(n) AS n") == 0


async def test_a_relationship_with_endpoints_outside_their_namespaces_is_refused(
    graph: tuple[GraphDriver, DomainPackLoader],
) -> None:
    driver, _ = graph
    await handle_sync(TENANT, {"entity_type": "aging-caves", "batch": [{"cave_id": ref} for ref in CAVES]})
    await handle_sync(TENANT, {"entity_type": "affineurs", "batch": [{"affineur_id": AFFINEUR}]})
    with pytest.raises(ValidationError, match="from_id"):
        await handle_sync(TENANT, {"entity_type": "tended-by", "batch": [_row(from_id="101")]})
    with pytest.raises(ValidationError, match="to_id"):
        await handle_sync(TENANT, {"entity_type": "tended-by", "batch": [_row(to_id=CAVES[1])]})
    assert await _count(driver, "MATCH ()-[r]->() RETURN count(r) AS n") == 0
