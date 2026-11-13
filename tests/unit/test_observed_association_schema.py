"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, config, schema, sync, relationship, evidence, identity, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_observed_association_schema.py
QM.POC0.TENANT_ASSOCIATION_MAPPING.004 without Neo4j: the tenant spec's
``observedfrom`` (provider association → relationship), ``refnamespace`` (record
id → graph ref) and ``evidencerelevance`` (approved relevance) declarations, as
``DomainSpec`` validates them at load (engine/config/tenant_mapping.py), the
write-side ref check (engine/sync/generator.py ``ref_namespace_defects``) and the
stage-gated relevance loader (engine/scoring/graph_evidence.py).

Vocabulary is synthetic, built at test time from tests/fixtures/domains: the
provider token, object types, category and type id below are placeholders, not
any tenant's or provider's configuration.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from engine.config.schema import DomainSpec, ObservedAssociationSourceSpec, SyncEndpointSpec
from engine.config.settings import settings
from engine.config.tenant_mapping import declared_ref_namespace, relevance_is_active, spec_version_stage
from engine.handlers import ValidationError, handle_sync, init_dependencies
from engine.scoring.graph_evidence import approved_relevance_from_spec
from engine.state import get_state
from engine.sync.generator import (
    ref_namespace_defects,
    relationship_merge_query,
    relationship_precheck_query,
    resolve_relationship_declaration,
)
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE: dict[str, Any] = {
    "provider": "synthcrm",
    "fromobjecttype": "caves",
    "toobjecttype": "affineurs",
    "associationcategory": "SYNTH_DEFINED",
    "associationtypeid": 7,
}


def _raw(*, observedfrom: Any = None, version: str = "1.1.0-reviewed") -> dict[str, Any]:
    """cheese-aging with namespaced node endpoints and one observed relationship endpoint."""
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text())
    raw["domain"]["version"] = version
    endpoints = raw["sync"]["endpoints"]
    endpoints[0]["refnamespace"] = "creamery.cave"
    endpoints.append(
        {
            "path": "/v1/sync/affineurs",
            "method": "POST",
            "targetnode": "Affineur",
            "idproperty": "affineur_id",
            "batchstrategy": "UNWINDMERGE",
            "refnamespace": "creamery.affineur",
        }
    )
    relationship: dict[str, Any] = {
        "path": "/v1/sync/tended-by",
        "method": "POST",
        "targetedge": "TENDED_BY",
        "fromnode": "AgingCave",
        "tonode": "Affineur",
        "batchstrategy": "UNWINDMERGE",
    }
    relationship["observedfrom"] = [dict(SOURCE)] if observedfrom is None else observedfrom
    endpoints.append(relationship)
    return raw


def _endpoint(raw: dict[str, Any], path: str) -> dict[str, Any]:
    return next(e for e in raw["sync"]["endpoints"] if e["path"] == path)


def _edge(raw: dict[str, Any], edge_type: str) -> dict[str, Any]:
    return next(e for e in raw["ontology"]["edges"] if e["type"] == edge_type)


def _refused(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(PydanticValidationError, match=match):
        DomainSpec.model_validate(raw)


def _spec_endpoint(spec: DomainSpec, path: str) -> SyncEndpointSpec:
    assert spec.sync is not None
    return next(e for e in spec.sync.endpoints if e.path == path)


# ── C-T1 / C-T2 / C-T3 / C-T4: the observation block itself ───────────────────


def test_a_valid_observation_block_parses_and_round_trips() -> None:
    spec = DomainSpec.model_validate(_raw())
    [source] = _spec_endpoint(spec, "/v1/sync/tended-by").observedfrom or []
    assert source.model_dump() == SOURCE
    assert source.key == ("synthcrm", "caves", "affineurs", "SYNTH_DEFINED", 7)
    with pytest.raises(PydanticValidationError, match="frozen"):
        source.associationtypeid = 8  # type: ignore[misc]


def test_an_unknown_key_inside_an_entry_is_refused() -> None:
    _refused(_raw(observedfrom=[{**SOURCE, "relationtype": "TENDED_BY"}]), "Extra inputs are not permitted")


@pytest.mark.parametrize("type_id", [True, "7", 0, -1, 3.0], ids=["bool", "quoted", "zero", "negative", "float"])
def test_the_association_type_id_is_a_strict_positive_integer(type_id: Any) -> None:
    _refused(_raw(observedfrom=[{**SOURCE, "associationtypeid": type_id}]), "associationtypeid")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("provider", "SynthCRM"),
        ("provider", ""),
        ("provider", "x"),
        ("fromobjecttype", "caves/1"),
        ("toobjecttype", "../affineurs"),
        ("toobjecttype", "Affineurs"),
        ("associationcategory", "synth_defined"),
        ("associationcategory", ""),
    ],
)
def test_each_grammar_refuses_a_malformed_value(key: str, value: str) -> None:
    _refused(_raw(observedfrom=[{**SOURCE, key: value}]), key)


@pytest.mark.parametrize("key", sorted(SOURCE))
def test_every_key_is_required(key: str) -> None:
    entry = {k: v for k, v in SOURCE.items() if k != key}
    _refused(_raw(observedfrom=[entry]), key)


def test_a_provider_custom_object_type_is_accepted() -> None:
    spec = DomainSpec.model_validate(_raw(observedfrom=[{**SOURCE, "toobjecttype": "2-1234"}]))
    [source] = _spec_endpoint(spec, "/v1/sync/tended-by").observedfrom or []
    assert source.toobjecttype == "2-1234"


# ── C-T5 / C-T6: the block only sits on a valid relationship endpoint ──────────


def test_an_observation_block_on_a_node_endpoint_is_refused() -> None:
    raw = _raw()
    raw["sync"]["endpoints"][0]["observedfrom"] = [dict(SOURCE, associationtypeid=8)]
    _refused(raw, "targetedge, fromnode and tonode")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        # WheelBatch has no refnamespace, so only the relationship rule can refuse it.
        ({"targetnode": "WheelBatch"}, "both targetnode and targetedge"),
        ({"batchstrategy": "UNWINDMATCHSET"}, "UNWINDMERGE"),
        ({"targetedge": "UNDECLARED"}, "exactly one edge"),
        ({"fromnode": "Affineur", "tonode": "AgingCave"}, "exactly one edge"),
        ({"targetedge": "SHARES_CULTURE_WITH", "tonode": "AgingCave"}, "DIRECTED and managedby sync"),
    ],
    ids=["targetnode-and-targetedge", "match-set", "undeclared-edge", "reversed", "undirected-gds"],
)
def test_an_observation_block_on_an_invalid_relationship_endpoint_is_refused(
    change: dict[str, Any], match: str
) -> None:
    raw = _raw()
    _endpoint(raw, "/v1/sync/tended-by").update(change)
    _refused(raw, match)


def test_an_observed_edge_must_be_sync_managed() -> None:
    raw = _raw()
    _edge(raw, "TENDED_BY")["managedby"] = "api"
    _refused(raw, "DIRECTED and managedby sync")


# ── C-T7 / C-T8: one observation, one relationship ─────────────────────────────


def test_a_duplicate_association_within_one_endpoint_is_refused() -> None:
    _refused(_raw(observedfrom=[dict(SOURCE), dict(SOURCE)]), "one association maps to at most one relationship")


def test_a_duplicate_association_across_two_endpoints_is_refused() -> None:
    raw = _raw()
    second = copy.deepcopy(_endpoint(raw, "/v1/sync/tended-by"))
    second["path"] = "/v1/sync/tended-by-again"
    raw["sync"]["endpoints"].append(second)
    _refused(raw, "already mapped by '/v1/sync/tended-by'")


def test_several_distinct_associations_may_feed_one_edge() -> None:
    entries = [
        dict(SOURCE),
        {**SOURCE, "associationcategory": "SYNTH_USER_DEFINED"},
        {**SOURCE, "associationtypeid": 8},
        {**SOURCE, "fromobjecttype": "affineurs", "toobjecttype": "caves"},
    ]
    spec = DomainSpec.model_validate(_raw(observedfrom=entries))
    assert len(_spec_endpoint(spec, "/v1/sync/tended-by").observedfrom or []) == 4


def test_an_empty_observation_list_is_refused_and_an_absent_one_accepted() -> None:
    _refused(_raw(observedfrom=[]), "non-empty list")
    raw = _raw()
    del _endpoint(raw, "/v1/sync/tended-by")["observedfrom"]
    assert _spec_endpoint(DomainSpec.model_validate(raw), "/v1/sync/tended-by").observedfrom is None


@pytest.mark.parametrize("strict", [True, False])
def test_validation_runs_whatever_the_strict_validation_flag(monkeypatch: pytest.MonkeyPatch, strict: bool) -> None:
    monkeypatch.setattr(settings, "domain_strict_validation", strict)
    _refused(_raw(observedfrom=[dict(SOURCE), dict(SOURCE)]), "at most one relationship")
    _refused(_raw(observedfrom=[]), "non-empty list")


# ── D-13: refnamespace is the one node identity declaration ────────────────────


def test_observed_labels_must_declare_a_ref_namespace() -> None:
    raw = _raw()
    del _endpoint(raw, "/v1/sync/affineurs")["refnamespace"]
    _refused(raw, "observed node 'Affineur' declares no refnamespace")


@pytest.mark.parametrize("namespace", ["Creamery", "creamery:cave", "", "a" * 65, "creamery cave"])
def test_a_malformed_ref_namespace_is_refused(namespace: str) -> None:
    raw = _raw()
    _endpoint(raw, "/v1/sync/affineurs")["refnamespace"] = namespace
    _refused(raw, "refnamespace")


def test_a_ref_namespace_on_a_relationship_endpoint_is_refused() -> None:
    raw = _raw()
    _endpoint(raw, "/v1/sync/tended-by")["refnamespace"] = "creamery.edge"
    _refused(raw, "node endpoints \\(targetnode\\) only")


@pytest.mark.parametrize("second_namespace", ["creamery.other", None], ids=["conflicting", "missing"])
def test_every_endpoint_of_a_namespaced_label_must_agree(second_namespace: str | None) -> None:
    raw = _raw()
    patch: dict[str, Any] = {
        "path": "/v1/sync/affineurs-patch",
        "method": "PATCH",
        "targetnode": "Affineur",
        "idproperty": "affineur_id",
        "batchstrategy": "UNWINDMATCHSET",
    }
    if second_namespace:
        patch["refnamespace"] = second_namespace
    raw["sync"]["endpoints"].append(patch)
    _refused(raw, "declares the same refnamespace")


def test_two_labels_cannot_share_a_ref_namespace() -> None:
    raw = _raw()
    _endpoint(raw, "/v1/sync/affineurs")["refnamespace"] = "creamery.cave"
    _refused(raw, "declared for both 'Affineur' and 'AgingCave'")


def test_a_namespaced_label_is_not_written_by_child_sync() -> None:
    raw = _raw()
    raw["sync"]["endpoints"][0]["childsync"] = [
        {
            "field": "affineurs",
            "targetnode": "Affineur",
            "targetid": "affineur_id",
            "edgetype": "TENDED_BY",
            "edgedirection": "parenttochild",
        }
    ]
    _refused(raw, "not by childsync or taxonomyedges")


def test_the_declaration_carries_the_namespaces_and_is_otherwise_unchanged() -> None:
    with_block = DomainSpec.model_validate(_raw())
    raw = _raw()
    del _endpoint(raw, "/v1/sync/tended-by")["observedfrom"]
    without_block = DomainSpec.model_validate(raw)
    a = resolve_relationship_declaration(with_block, _spec_endpoint(with_block, "/v1/sync/tended-by"))
    b = resolve_relationship_declaration(without_block, _spec_endpoint(without_block, "/v1/sync/tended-by"))
    assert a == b
    assert (a.from_ref_namespace, a.to_ref_namespace) == ("creamery.cave", "creamery.affineur")
    assert relationship_precheck_query(a) == relationship_precheck_query(b)
    assert relationship_merge_query(a) == relationship_merge_query(b)
    assert declared_ref_namespace(with_block, "WheelBatch") is None


# ── D-13 on the write side: ids outside the namespace never reach the graph ────


def test_node_rows_must_carry_ids_in_the_declared_namespace() -> None:
    spec = DomainSpec.model_validate(_raw())
    endpoint = _spec_endpoint(spec, "/v1/sync/affineurs")
    good = {"affineur_id": "creamery.affineur:7"}
    assert ref_namespace_defects(spec, endpoint, [good]) == []
    bad = [
        {"affineur_id": "7"},
        {"affineur_id": "creamery.cave:7"},
        {"affineur_id": "creamery.affineur:"},
        {"affineur_id": "creamery.affineur:7 8"},
        {"affineur_id": 7},
        {},
    ]
    defects = ref_namespace_defects(spec, endpoint, [good, *bad])
    assert [d.split(":", 1)[0] for d in defects] == [f"row {i}" for i in range(1, 7)]


def test_relationship_rows_must_carry_ids_in_both_namespaces() -> None:
    spec = DomainSpec.model_validate(_raw())
    endpoint = _spec_endpoint(spec, "/v1/sync/tended-by")
    good = {"from_id": "creamery.cave:101", "to_id": "creamery.affineur:7"}
    assert ref_namespace_defects(spec, endpoint, [good]) == []
    swapped = {"from_id": "creamery.affineur:7", "to_id": "creamery.cave:101"}
    assert len(ref_namespace_defects(spec, endpoint, [swapped])) == 2


def test_labels_without_a_declaration_are_unconstrained() -> None:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text())
    spec = DomainSpec.model_validate(raw)
    assert ref_namespace_defects(spec, _spec_endpoint(spec, "/v1/sync/aging-caves"), [{"cave_id": "101"}]) == []


@pytest.fixture
def _clean_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


@pytest.mark.usefixtures("_clean_state")
async def test_a_node_sync_outside_the_namespace_is_refused_before_any_graph_write() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    driver.execute_write = AsyncMock()
    loader = MagicMock()
    loader.load_domain.return_value = DomainSpec.model_validate(_raw())
    init_dependencies(driver, loader)
    with pytest.raises(ValidationError, match="is not a graph ref creamery.affineur:<id>"):
        await handle_sync("cheese-aging", {"entity_type": "affineurs", "batch": [{"affineur_id": "7"}]})
    driver.execute_query.assert_not_awaited()
    driver.execute_write.assert_not_awaited()


# ── C-T9: every previously valid spec still validates, unchanged ───────────────


def _shipped_specs() -> list[Path]:
    return sorted([*DESIGN_PACKS_ROOT.glob("*/spec.yaml"), *SYNTHETIC_DOMAINS_ROOT.glob("*/spec.yaml")])


def test_shipped_specs_still_validate_and_declare_no_mapping() -> None:
    specs = _shipped_specs()
    assert specs, "no shipped specs found"
    for path in specs:
        spec = DomainSpec.model_validate(yaml.safe_load(path.read_text()))
        for endpoint in spec.sync.endpoints if spec.sync else []:
            assert (endpoint.observedfrom, endpoint.refnamespace) == (None, None), path
        assert all(edge.evidencerelevance is None for edge in spec.ontology.edges), path


# ── D-8 / D-8b / C-T12 / C-T13: approved relevance ─────────────────────────────


def _with_relevance(value: Any, *, version: str = "1.1.0-reviewed") -> dict[str, Any]:
    raw = _raw(version=version)
    _edge(raw, "TENDED_BY")["evidencerelevance"] = value
    return raw


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_relevance_is_a_finite_number_in_the_unit_interval(value: Any) -> None:
    _refused(_with_relevance(value), "evidencerelevance")


def test_relevance_on_an_edge_no_relationship_endpoint_writes_is_refused() -> None:
    raw = _raw()
    _edge(raw, "SHARES_CULTURE_WITH")["evidencerelevance"] = 0.5
    _refused(raw, "no relationship sync endpoint writes it")


@pytest.mark.parametrize("second", [0.4, None], ids=["disagreeing", "undeclared"])
def test_every_signature_of_a_type_declares_the_same_relevance(second: float | None) -> None:
    raw = _with_relevance(0.9)
    edge = {
        "type": "TENDED_BY",
        "from": "WheelBatch",
        "to": "Affineur",
        "direction": "DIRECTED",
        "category": _edge(raw, "TENDED_BY")["category"],
        "managedby": "sync",
    }
    if second is not None:
        edge["evidencerelevance"] = second
    raw["ontology"]["edges"].append(edge)
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/batch-tended-by",
            "method": "POST",
            "targetedge": "TENDED_BY",
            "fromnode": "WheelBatch",
            "tonode": "Affineur",
            "batchstrategy": "UNWINDMERGE",
        }
    )
    _refused(raw, "declares the same evidencerelevance")


@pytest.mark.parametrize(
    ("version", "active"),
    [
        ("1.1.0-reviewed", True),
        ("1.1.0-production", True),
        ("1.1.0-proposed", False),
        ("0.4.0-inferred", False),
        ("1.1.0", False),
    ],
)
def test_relevance_reaches_scoring_only_from_a_reviewed_or_production_spec(version: str, active: bool) -> None:
    spec = DomainSpec.model_validate(_with_relevance(0.75, version=version))
    assert relevance_is_active(spec) is active
    relevance = approved_relevance_from_spec(spec)
    assert dict(relevance) == ({"TENDED_BY": 0.75} if active else {})
    with pytest.raises(TypeError):
        relevance["TENDED_BY"] = 1.0  # type: ignore[index]


def test_a_spec_without_relevance_yields_an_empty_map() -> None:
    assert dict(approved_relevance_from_spec(DomainSpec.model_validate(_raw()))) == {}


def test_the_version_stage_parser_reads_only_the_declared_grammar() -> None:
    assert spec_version_stage("2.0.1-production") == "production"
    assert spec_version_stage("2.0.1-seed") == "seed"
    assert spec_version_stage("2.0.1-approved") is None
    assert spec_version_stage("v2.0.1-reviewed") is None
    # A suffix after the stage is not that stage. Asserted on the parser, not via
    # DomainSpec: domain.version validation may refuse such a version outright,
    # which denies relevance too, earlier (RU-057 / CEG-COR-152).
    assert spec_version_stage("1.1.0-reviewed-hotfix") is None


def test_observation_and_relevance_models_are_separate() -> None:
    assert "evidencerelevance" not in ObservedAssociationSourceSpec.model_fields
