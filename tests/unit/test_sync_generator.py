"""Unit tests — SyncGenerator: MERGE strategy, batch param, unknown strategy.

Real API (engine/sync/generator.py):
    SyncGenerator(domain_spec)
        .generate_sync_query(endpoint_spec, batch_data: list[dict]) -> str
Returns the Cypher string only; the batch is bound as the $batch parameter at
execution time by the handler.
"""

from __future__ import annotations

import pytest

from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit


@pytest.fixture
def plasticos_spec():
    """Load plasticos spec, skip if not loadable."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT))
    try:
        return loader.load_domain("plasticos")
    except Exception:
        pytest.skip("plasticos domain spec not loadable with current schema")


def test_sync_generator_produces_merge_cypher(plasticos_spec):
    """Sync generator produces UNWIND + MERGE/MATCH Cypher."""
    from engine.sync.generator import SyncGenerator

    gen = SyncGenerator(plasticos_spec)
    if not plasticos_spec.sync or not plasticos_spec.sync.endpoints:
        pytest.skip("No sync endpoints in plasticos spec")
    ep = plasticos_spec.sync.endpoints[0]
    cypher = gen.generate_sync_query(ep, [{"facility_id": "test-1", "name": "Alpha"}])
    assert "MERGE" in cypher or "MATCH" in cypher
    assert "UNWIND" in cypher


def test_sync_generator_includes_batch_param(plasticos_spec):
    """Sync query references the $batch parameter."""
    from engine.sync.generator import SyncGenerator

    gen = SyncGenerator(plasticos_spec)
    if not plasticos_spec.sync or not plasticos_spec.sync.endpoints:
        pytest.skip("No sync endpoints in plasticos spec")
    ep = plasticos_spec.sync.endpoints[0]
    cypher = gen.generate_sync_query(ep, [{"facility_id": "f1"}])
    assert "$batch" in cypher


def test_unknown_strategy_raises(plasticos_spec):
    """An endpoint with an unknown batch strategy raises ValueError."""
    from engine.sync.generator import SyncGenerator

    gen = SyncGenerator(plasticos_spec)
    if not plasticos_spec.sync or not plasticos_spec.sync.endpoints:
        pytest.skip("No sync endpoints in plasticos spec")
    ep = plasticos_spec.sync.endpoints[0].model_copy(deep=True)
    object.__setattr__(ep, "batchstrategy", "NONEXISTENT_STRATEGY")
    with pytest.raises(ValueError):
        gen.generate_sync_query(ep, [{"facility_id": "f1"}])


# ── RU-060 / CEG-COR-169: UNWINDMATCHSET reports the rows it matched ──


def _match_set_endpoint(plasticos_spec, **updates):
    from engine.config.schema import SyncStrategy

    ep = plasticos_spec.sync.endpoints[0]
    return ep.model_copy(update={"batchstrategy": SyncStrategy.UNWINDMATCHSET, **updates})


@pytest.mark.finding("CEG-COR-169")
@pytest.mark.parametrize(("projection", "row"), [(True, "row.node"), (False, "row")])
def test_unwind_match_set_returns_matched_count(plasticos_spec, monkeypatch, projection, row):
    from engine.config.settings import settings
    from engine.sync.generator import SyncGenerator

    monkeypatch.setattr(settings, "sync_declared_projection_enabled", projection)
    for ep in (_match_set_endpoint(plasticos_spec), _match_set_endpoint(plasticos_spec, fieldsupdated=["name"])):
        cypher = SyncGenerator(plasticos_spec).generate_sync_query(ep, [{"facility_id": "f1"}])
        assert cypher.startswith(f"UNWIND $batch AS row\nMATCH (n:Facility {{facility_id: {row}.facility_id}})")
        assert cypher.endswith("\nRETURN count(n) AS synced_count")


@pytest.mark.finding("CEG-COR-169")
@pytest.mark.parametrize(("projection", "row"), [(True, "row.node"), (False, "row")])
def test_unwind_merge_unchanged_and_flag_off_restores_match_set(plasticos_spec, monkeypatch, projection, row):
    from engine.config.settings import settings
    from engine.sync.generator import SyncGenerator

    monkeypatch.setattr(settings, "sync_declared_projection_enabled", projection)
    gen = SyncGenerator(plasticos_spec)
    merge = gen.generate_sync_query(plasticos_spec.sync.endpoints[0], [{"facility_id": "f1"}])
    assert "RETURN" not in merge

    monkeypatch.setattr(settings, "sync_count_from_write_result", False)
    cypher = gen.generate_sync_query(_match_set_endpoint(plasticos_spec), [{"facility_id": "f1"}])
    assert "RETURN" not in cypher
    assert cypher.endswith(f"SET n += {row}, n._tenant = $tenant")


def _endpoint(spec, path):
    return next(ep for ep in spec.sync.endpoints if ep.path == path)


@pytest.mark.parametrize(("projection", "row"), [(True, "row.node"), (False, "row")])
def test_plasticos_supply_opportunities_sync_links_fulfilled_polymer(plasticos_spec, monkeypatch, projection, row):
    """RG-062 (RU-048 / CEG-COR-071): a synced SupplyOpportunity carries its FULFILLS_POLYMER edge."""
    from engine.config.settings import settings
    from engine.sync.generator import SyncGenerator

    monkeypatch.setattr(settings, "sync_declared_projection_enabled", projection)
    ep = _endpoint(plasticos_spec, "/v1/sync/supply-opportunities")
    cypher = SyncGenerator(plasticos_spec).generate_sync_query(ep, [{"opportunity_id": "so1", "polymer_type": "PET"}])
    assert f"OPTIONAL MATCH (tax:PolymerFamily {{code: {row}.polymer_type}})" in cypher
    assert "MERGE (n)-[:FULFILLS_POLYMER]->(tax)" in cypher


def test_plasticos_facilities_sync_links_processed_polymers(plasticos_spec):
    """RG-062 (RU-048 / CEG-COR-071): a synced Facility carries one PROCESSES edge per listed polymer.

    Declared projection (default): the parent receives only ``row.node``, the
    children come from ``row.children`` and PolymerFamily, a static taxonomy,
    is written only when the MERGE creates it.
    """
    from engine.sync.generator import SyncGenerator

    ep = _endpoint(plasticos_spec, "/v1/sync/facilities")
    cypher = SyncGenerator(plasticos_spec).generate_sync_query(
        ep, [{"facility_id": "f1", "processed_polymers": [{"code": "PET"}, {"code": "HDPE"}]}]
    )
    assert "SET n += row.node, n._tenant = $tenant" in cypher
    assert "row{.*" not in cypher
    assert "FOREACH (child_row IN coalesce(row.children.processed_polymers, []) |" in cypher
    assert "MERGE (c:PolymerFamily {code: child_row.code})" in cypher
    assert "ON CREATE SET c += child_row, c._tenant = $tenant" in cypher
    assert "MERGE (n)-[:PROCESSES]->(c)" in cypher


def test_plasticos_facilities_sync_legacy_contract_behind_flag(plasticos_spec, monkeypatch):
    """C-21: sync_declared_projection_enabled=False restores the Stage 2 ``row{.*}`` Cypher."""
    from engine.config.settings import settings
    from engine.sync.generator import SyncGenerator

    monkeypatch.setattr(settings, "sync_declared_projection_enabled", False)
    ep = _endpoint(plasticos_spec, "/v1/sync/facilities")
    cypher = SyncGenerator(plasticos_spec).generate_sync_query(
        ep, [{"facility_id": "f1", "processed_polymers": [{"code": "PET"}, {"code": "HDPE"}]}]
    )
    assert "SET n += row{.*, processed_polymers: null}" in cypher
    assert "FOREACH (child_row IN coalesce(row.processed_polymers, []) |" in cypher
    assert "MERGE (c:PolymerFamily {code: child_row.code})" in cypher
    assert "MERGE (n)-[:PROCESSES]->(c)" in cypher
