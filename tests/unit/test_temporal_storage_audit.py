"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, health, audit, temporal, read-only]
owner: engine-team
status: active
--- /L9_META ---

Read-only detector of string-valued declared temporals (ceg-scoring-audit L1,
D1). Runbook: docs/runbooks/temporal-storage-audit.md.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.health.temporal_storage_audit import audit_temporal_storage, build_audit_queries
from tests.fixtures import typed_sync as fx

pytestmark = pytest.mark.unit


def _temporal_raw() -> dict[str, Any]:
    raw = fx.orbital_raw()
    fx.node_props(raw, "OrbitalDepot").extend(
        [
            {"name": "opened_at", "type": "datetime"},
            {"name": "opened_on", "type": "date"},
            {"name": "service_window", "type": "duration"},
            {"name": "inspections", "type": "list", "items": {"type": "date"}},
            {"name": "throughput", "type": "float"},
        ]
    )
    fx.node_props(raw, "PropellantGrade").append({"name": "certified_at", "type": "datetime"})
    return raw


def test_the_detector_reads_every_declared_temporal_of_plasticos() -> None:
    queries = {(q.owner, q.label): q for q in build_audit_queries(fx.plasticos())}
    facility = queries[("node", "Facility")]
    assert [p.name for p in facility.properties] == ["last_transaction_date"]
    assert facility.cypher == (
        "MATCH (e:Facility) RETURN count(CASE WHEN e.last_transaction_date IS :: STRING NOT NULL THEN 1 END) AS c0"
    )
    edge = queries[("relationship", "TRANSACTED_WITH")]
    assert edge.cypher == (
        "MATCH (:Facility)-[e:TRANSACTED_WITH]->(:Facility) "
        "RETURN count(CASE WHEN e.transaction_date IS :: STRING NOT NULL THEN 1 END) AS c0"
    )
    for query in queries.values():
        assert not any(word in query.cypher.upper() for word in ("SET ", "CREATE", "MERGE", "DELETE", "REMOVE"))


def test_the_detector_covers_list_temporals_skips_non_temporals_and_groups_by_label() -> None:
    queries = {q.label: q for q in build_audit_queries(fx.spec(_temporal_raw()))}
    depot = queries["OrbitalDepot"]
    assert [p.name for p in depot.properties] == ["opened_at", "opened_on", "service_window", "inspections"]
    assert (
        "count(CASE WHEN e.inspections IS :: LIST<ANY> NOT NULL AND any(element IN e.inspections "
        "WHERE element IS :: STRING NOT NULL) THEN 1 END) AS c3"
    ) in depot.cypher
    assert "throughput" not in depot.cypher
    assert [p.name for p in queries["PropellantGrade"].properties] == ["certified_at"]


async def test_the_detector_aggregates_counts_through_execute_read_only() -> None:
    graph = MagicMock()
    graph.execute_read = AsyncMock(
        side_effect=lambda cypher, *_a, **_k: [{"c0": 3}] if cypher.startswith("MATCH (e:Facility)") else []
    )
    graph.execute_query = AsyncMock(side_effect=AssertionError("the detector must not use the write path"))
    graph.execute_write = AsyncMock(side_effect=AssertionError("the detector must not write"))
    report = await audit_temporal_storage(graph, fx.plasticos())
    assert report["database"] == "plasticos"
    assert report["string_valued_total"] == 3
    assert report["conversion_required"] is True
    assert {
        "owner": "node",
        "label": "Facility",
        "property": "last_transaction_date",
        "declared": "datetime",
        "endpoints": None,
        "string_valued": 3,
    } in report["findings"]
    edge = next(f for f in report["findings"] if f["property"] == "transaction_date")
    assert edge["endpoints"] == "Facility->Facility"
    assert {f["property"]: f["string_valued"] for f in report["findings"]}["transaction_date"] == 0
    assert all(call.kwargs["database"] == "plasticos" for call in graph.execute_read.await_args_list)


async def test_no_string_valued_temporal_means_no_conversion() -> None:
    graph = MagicMock()
    graph.execute_read = AsyncMock(return_value=[{"c0": 0}])
    report = await audit_temporal_storage(graph, fx.plasticos())
    assert report["string_valued_total"] == 0
    assert report["conversion_required"] is False
    assert report["checked"] == len(report["findings"]) >= 2


def test_the_audit_tool_exit_code_reports_conversion_need(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import audit_temporal_storage as tool

    async def clean(_tenants: list[str], _root: str) -> list[dict[str, Any]]:
        return [{"conversion_required": False}]

    async def dirty(_tenants: list[str], _root: str) -> list[dict[str, Any]]:
        return [{"conversion_required": False}, {"conversion_required": True}]

    monkeypatch.setattr(tool, "_audit", clean)
    assert tool.main(["plasticos"]) == tool.EXIT_CLEAN
    monkeypatch.setattr(tool, "_audit", dirty)
    assert tool.main(["plasticos", "other"]) == tool.EXIT_STRING_VALUED


def test_the_audit_tool_leaves_pack_root_precedence_to_the_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    # CodeRabbit on #563: a DOMAINS_ROOT default shadowed the loader's DOMAIN_SPECS_PATH alias.
    from tools import audit_temporal_storage as tool

    seen: list[str | None] = []

    async def record(_tenants: list[str], root: str | None) -> list[dict[str, Any]]:
        seen.append(root)
        return [{"conversion_required": False}]

    monkeypatch.setattr(tool, "_audit", record)
    tool.main(["plasticos"])
    tool.main(["plasticos", "--domains-root", "/packs"])
    assert seen == [None, "/packs"]


def test_a_reused_relationship_type_is_audited_per_declared_endpoints() -> None:
    # Codex on #565: one type, different from/to pairs, different property types.
    import yaml

    from engine.config.schema import DomainSpec
    from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

    raw = yaml.safe_load((DESIGN_PACKS_ROOT / "plasticos/spec.yaml").read_text())
    base = {"type": "TRANSACTED_WITH", "from": "Facility", "direction": "DIRECTED", "category": "transaction"}
    raw["ontology"]["edges"] += [
        {
            **base,
            "to": "SupplyOpportunity",
            "managedby": "sync",
            "properties": [{"name": "transaction_date", "type": "string"}],
        },
        {
            **base,
            "to": "BuyerDemand",
            "managedby": "sync",
            "properties": [{"name": "transaction_date", "type": "date"}],
        },
    ]
    edges = [q for q in build_audit_queries(DomainSpec.model_validate(raw)) if q.owner == "relationship"]
    assert [q.cypher.split(" RETURN ")[0] for q in edges] == [
        "MATCH (:Facility)-[e:TRANSACTED_WITH]->(:Facility)",
        "MATCH (:Facility)-[e:TRANSACTED_WITH]->(:BuyerDemand)",
    ]  # the string-declared Facility->SupplyOpportunity edge is never counted as a stale temporal
