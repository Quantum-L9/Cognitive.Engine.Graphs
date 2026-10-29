"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, sync, relationship, evidence, scoring, tenancy, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_graph_evidence_score.py
QM.POC0.CEG.GRAPH_EVIDENCE.002A against a real Neo4j: relationship evidence sync
materialises one declared, evidenced edge in the tenant database, and the local
``handle_score`` reads it back through ``GraphEvidenceBinding``.

The two vocabularies are the repository's synthetic verification domains
(``tests/fixtures/domains``), copied under new ids with one relationship sync
endpoint added for an edge their ontology already declares. The same runtime
code serves both; no named product domain is involved.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from neo4j.exceptions import ConstraintError

import engine.handlers as handlers_module
from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import ExecutionError, ValidationError, handle_score, handle_sync, init_dependencies
from engine.scoring.evidence_priority import approved_relevance_map
from engine.scoring.graph_evidence import GraphEvidenceBinding
from engine.state import get_state
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

VOCABULARIES: dict[str, dict[str, Any]] = {
    "it-cheese-aging": {
        "source": "cheese-aging",
        "extra_endpoints": [
            {"path": "/v1/sync/affineurs", "targetnode": "Affineur", "idproperty": "affineur_id"},
            {"path": "/v1/sync/tended-by", "targetedge": "TENDED_BY", "fromnode": "AgingCave", "tonode": "Affineur"},
        ],
        "from_type": "aging-caves",
        "from_label": "AgingCave",
        "from_id": "cave_id",
        "to_type": "affineurs",
        "to_label": "Affineur",
        "to_id": "affineur_id",
        "edge_entity_type": "tended-by",
        "edge_type": "TENDED_BY",
        "subjects": ["creamery.cave:101", "creamery.cave:102"],
        "object": "creamery.affineur:7",
    },
    "it-orbital-logistics": {
        "source": "orbital-logistics",
        "extra_endpoints": [
            {
                "path": "/v1/sync/stocks",
                "targetedge": "STOCKS",
                "fromnode": "OrbitalDepot",
                "tonode": "PropellantGrade",
            },
        ],
        "from_type": "orbital-depots",
        "from_label": "OrbitalDepot",
        "from_id": "depot_id",
        "to_type": "propellant-grades",
        "to_label": "PropellantGrade",
        "to_id": "grade_code",
        "edge_entity_type": "stocks",
        "edge_type": "STOCKS",
        "subjects": ["ledger.depot:1", "ledger.depot:2"],
        "object": "ledger.grade:lox",
    },
}
TENANT_A = "it-cheese-aging"
TENANT_B = "it-orbital-logistics"


@pytest.fixture(scope="module")
def domains_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("edge-domains")
    for domain_id, vocabulary in VOCABULARIES.items():
        spec = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / vocabulary["source"] / "spec.yaml").read_text())
        spec["domain"]["id"] = domain_id
        for endpoint in vocabulary["extra_endpoints"]:
            spec["sync"]["endpoints"].append({"method": "POST", "batchstrategy": "UNWINDMERGE", **endpoint})
        (root / domain_id).mkdir()
        (root / domain_id / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        source_dir = SYNTHETIC_DOMAINS_ROOT / vocabulary["source"]
        for extra in source_dir.iterdir():
            if extra.name != "spec.yaml" and extra.is_file():
                shutil.copy(extra, root / domain_id / extra.name)
    return root


async def _wipe(driver: GraphDriver, database: str) -> None:
    await driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=database)


@pytest_asyncio.fixture
async def graph(
    graph_driver: GraphDriver, domains_root: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[GraphDriver, None]:
    monkeypatch.setattr(settings, "relationship_sync_enabled", True)
    for tenant in VOCABULARIES:
        await graph_driver.execute_query(f"CREATE DATABASE `{tenant}` IF NOT EXISTS WAIT", database="system")
        await _wipe(graph_driver, tenant)
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(domains_root)))
    yield graph_driver
    for tenant in VOCABULARIES:
        await _wipe(graph_driver, tenant)
    get_state().reset()


async def _seed_nodes(tenant: str) -> None:
    v = VOCABULARIES[tenant]
    await handle_sync(tenant, {"entity_type": v["from_type"], "batch": [{v["from_id"]: ref} for ref in v["subjects"]]})
    await handle_sync(tenant, {"entity_type": v["to_type"], "batch": [{v["to_id"]: v["object"]}]})


def _edge_row(tenant: str, **overrides: Any) -> dict[str, Any]:
    v = VOCABULARIES[tenant]
    row: dict[str, Any] = {
        "from_id": v["subjects"][0],
        "to_id": v["object"],
        "evidence_ref": f"evidence:{tenant}:1",
        "source_ref": "source:crm-note:42",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
        "source_revision": "rev-2026-10-25",
    }
    row.update(overrides)
    return row


async def _sync_edges(tenant: str, *rows: dict[str, Any]) -> dict[str, Any]:
    return await handle_sync(tenant, {"entity_type": VOCABULARIES[tenant]["edge_entity_type"], "batch": list(rows)})


async def _edges(driver: GraphDriver, tenant: str) -> list[dict[str, Any]]:
    return await driver.execute_query(
        "MATCH (a)-[r]->(b) RETURN labels(a) AS from_labels, type(r) AS type, labels(b) AS to_labels, "
        "properties(r) AS props ORDER BY props.evidence_ref",
        database=tenant,
    )


def _binding(driver: GraphDriver, tenant: str) -> GraphEvidenceBinding:
    return GraphEvidenceBinding(driver, approved_relevance_map({VOCABULARIES[tenant]["edge_type"]: 1.0}))


async def _score(driver: GraphDriver, tenant: str) -> dict[str, Any]:
    subjects = VOCABULARIES[tenant]["subjects"]
    return await handle_score(
        tenant, {"entity_refs": subjects, "top_n": len(subjects)}, binding=_binding(driver, tenant)
    )


# ── A. Actual graph evidence ───────────────────────────────────────────────────


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
async def test_relationship_is_materialised_with_its_evidence(graph: GraphDriver, tenant: str) -> None:
    v = VOCABULARIES[tenant]
    await _seed_nodes(tenant)
    result = await _sync_edges(tenant, _edge_row(tenant))
    assert result == {
        "status": "success",
        "entity_type": v["edge_entity_type"],
        "synced_count": 1,
        "created_count": 1,
        "replayed_count": 0,
    }
    [edge] = await _edges(graph, tenant)
    assert edge["from_labels"] == [v["from_label"]]
    assert edge["type"] == v["edge_type"]
    assert edge["to_labels"] == [v["to_label"]]
    assert edge["props"] == {
        "evidence_ref": f"evidence:{tenant}:1",
        "source_ref": "source:crm-note:42",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
        "source_revision": "rev-2026-10-25",
        "_tenant": tenant,
        "_evidence_schema": "ceg.relationship_evidence.v1",
        "_subject_ref": v["subjects"][0],
        "_object_ref": v["object"],
    }


# ── B. Local score binding over the real graph ─────────────────────────────────


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
async def test_local_score_reads_the_real_graph_and_abstains_without_it(graph: GraphDriver, tenant: str) -> None:
    v = VOCABULARIES[tenant]
    await _seed_nodes(tenant)
    await _sync_edges(tenant, _edge_row(tenant))

    result = await _score(graph, tenant)
    assert result["status"] == "ok"
    assert result["evidence_reader"] == {"connected": True, "database": tenant}
    assert result["source_revision"] == "rev-2026-10-25"
    [scored] = result["scored"]
    assert scored["entity_ref"] == v["subjects"][0]
    assert scored["score"] == 80.0
    assert scored["factors"][0]["relationship_type"] == v["edge_type"]
    assert scored["factors"][0]["evidence_refs"] == [f"evidence:{tenant}:1"]
    # Same flat node, no relationship: abstains.
    assert [a["entity_ref"] for a in result["abstained"]] == [v["subjects"][1]]

    await graph.execute_write(cypher="MATCH ()-[r]->() DELETE r", parameters={}, database=tenant)
    removed = await _score(graph, tenant)
    assert removed["evidence_reader"]["connected"] is True
    assert removed["scored"] == []
    assert {a["entity_ref"] for a in removed["abstained"]} == set(v["subjects"])


# ── D. Negative evidence and replay ────────────────────────────────────────────


async def test_replay_is_idempotent_and_a_conflicting_revision_is_refused(graph: GraphDriver) -> None:
    await _seed_nodes(TENANT_A)
    await _sync_edges(TENANT_A, _edge_row(TENANT_A))
    first_score = await _score(graph, TENANT_A)

    replay = await _sync_edges(TENANT_A, _edge_row(TENANT_A), _edge_row(TENANT_A))
    assert replay["created_count"] == 0
    assert replay["replayed_count"] == 1
    assert len(await _edges(graph, TENANT_A)) == 1
    assert await _score(graph, TENANT_A) == first_score

    with pytest.raises(ValidationError, match="provenance is not overwritten"):
        await _sync_edges(TENANT_A, _edge_row(TENANT_A, confidence=0.99, source_revision="rev-other"))
    [edge] = await _edges(graph, TENANT_A)
    assert edge["props"]["confidence"] == 0.8
    assert edge["props"]["source_revision"] == "rev-2026-10-25"


@pytest.mark.parametrize(
    "overrides",
    [
        {"polarity": "negative"},
        {"assertion_status": "inferred"},
        {"assertion_status": "reported"},
        {"expires_at": "2020-01-01T00:00:00+00:00"},
    ],
    ids=["negative", "inferred", "reported", "stale"],
)
async def test_unqualified_stored_evidence_never_scores(graph: GraphDriver, overrides: dict[str, Any]) -> None:
    await _seed_nodes(TENANT_A)
    await _sync_edges(TENANT_A, _edge_row(TENANT_A, **overrides))
    assert len(await _edges(graph, TENANT_A)) == 1
    result = await _score(graph, TENANT_A)
    assert result["evidence_reader"]["connected"] is True
    assert result["scored"] == []


@pytest.mark.parametrize(
    ("row_overrides", "match"),
    [
        ({"to_id": "creamery.affineur:missing"}, "exactly one existing node"),
        ({"relationship_type": "OTHER"}, "unsupported keys"),
    ],
)
async def test_invalid_relationship_rows_write_nothing(
    graph: GraphDriver, row_overrides: dict[str, Any], match: str
) -> None:
    await _seed_nodes(TENANT_A)
    with pytest.raises(ValidationError, match=match):
        await _sync_edges(
            TENANT_A, _edge_row(TENANT_A, evidence_ref="evidence:ok"), _edge_row(TENANT_A, **row_overrides)
        )
    assert await _edges(graph, TENANT_A) == []


# ── E. Transactional integrity and evidence identity ───────────────────────────


async def test_a_failed_postcheck_rolls_back_every_write(graph: GraphDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_nodes(TENANT_A)
    # The post-write comparison sees evidence that differs from what was sent.
    monkeypatch.setattr(handlers_module, "stored_evidence", lambda stored: {"tampered": True})
    rows = [_edge_row(TENANT_A, evidence_ref=f"evidence:rollback:{i}") for i in range(3)]
    with pytest.raises(ExecutionError, match="rolled back"):
        await _sync_edges(TENANT_A, *rows)
    assert await _edges(graph, TENANT_A) == []


async def test_the_database_refuses_a_second_edge_for_one_evidence_ref(graph: GraphDriver) -> None:
    await _seed_nodes(TENANT_A)
    await _sync_edges(TENANT_A, _edge_row(TENANT_A))
    v = VOCABULARIES[TENANT_A]
    # Bypass every engine check: write the same (tenant, evidence_ref) on another endpoint pair.
    with pytest.raises(ConstraintError):
        await graph.execute_write(
            cypher=(
                f"MATCH (a:{v['from_label']} {{{v['from_id']}: $other}}), (b:{v['to_label']}) "
                f"CREATE (a)-[:{v['edge_type']} {{_tenant: $tenant, evidence_ref: $ref}}]->(b)"
            ),
            parameters={"other": v["subjects"][1], "tenant": TENANT_A, "ref": f"evidence:{TENANT_A}:1"},
            database=TENANT_A,
        )
    assert len(await _edges(graph, TENANT_A)) == 1


async def test_concurrent_conflicting_writers_never_commit_two_assertions(graph: GraphDriver) -> None:
    await _seed_nodes(TENANT_A)
    v = VOCABULARIES[TENANT_A]
    for round_ in range(5):
        ref = f"evidence:race:{round_}"
        outcomes = await asyncio.gather(
            _sync_edges(TENANT_A, _edge_row(TENANT_A, evidence_ref=ref, from_id=v["subjects"][0])),
            _sync_edges(TENANT_A, _edge_row(TENANT_A, evidence_ref=ref, from_id=v["subjects"][1])),
            return_exceptions=True,
        )
        successes = [o for o in outcomes if isinstance(o, dict)]
        refusals = [o for o in outcomes if isinstance(o, ValidationError)]
        assert (len(successes), len(refusals)) == (1, 1), outcomes
    edges = await _edges(graph, TENANT_A)
    assert sorted(e["props"]["evidence_ref"] for e in edges) == [f"evidence:race:{i}" for i in range(5)]


async def test_concurrent_identical_replays_record_one_edge(graph: GraphDriver) -> None:
    await _seed_nodes(TENANT_A)
    outcomes = await asyncio.gather(*(_sync_edges(TENANT_A, _edge_row(TENANT_A)) for _ in range(4)))
    assert all(o["status"] == "success" for o in outcomes)
    [edge] = await _edges(graph, TENANT_A)
    assert edge["props"]["source_revision"] == "rev-2026-10-25"


# ── C. Isolation ───────────────────────────────────────────


async def test_tenant_cannot_link_or_read_another_tenants_graph(graph: GraphDriver) -> None:
    await _seed_nodes(TENANT_A)
    await _seed_nodes(TENANT_B)
    b = VOCABULARIES[TENANT_B]
    await _sync_edges(TENANT_B, _edge_row(TENANT_B))

    # B's ids do not exist in A's database: refused, nothing written.
    with pytest.raises(ValidationError, match="exactly one existing node"):
        await _sync_edges(TENANT_A, _edge_row(TENANT_A, from_id=b["subjects"][0], to_id=b["object"]))
    # A node in A's database stamped for another tenant is never an endpoint.
    await graph.execute_write(
        cypher="CREATE (:Affineur {affineur_id: $id, _tenant: $other})",
        parameters={"id": "creamery.affineur:foreign", "other": TENANT_B},
        database=TENANT_A,
    )
    with pytest.raises(ValidationError, match="outside this tenant"):
        await _sync_edges(TENANT_A, _edge_row(TENANT_A, to_id="creamery.affineur:foreign"))
    assert await _edges(graph, TENANT_A) == []

    # Scoring A with B's subject ref reads A's database only: abstains, no B evidence.
    result = await handle_score(
        TENANT_A, {"entity_refs": [b["subjects"][0]], "top_n": 1}, binding=_binding(graph, TENANT_A)
    )
    assert result["scored"] == []
    assert f"evidence:{TENANT_B}:1" not in repr(result)
