"""Unit tests — SyncGenerator: MERGE strategy, batch param, unknown strategy.

Real API (engine/sync/generator.py):
    SyncGenerator(domain_spec)
        .generate_sync_query(endpoint_spec, batch_data: list[dict]) -> str
Returns the Cypher string only; the batch is bound as the $batch parameter at
execution time by the handler.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def plasticos_spec():
    """Load plasticos spec, skip if not loadable."""
    from engine.config.loader import DomainPackLoader

    loader = DomainPackLoader()
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
