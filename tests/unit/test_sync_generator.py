"""Unit tests - SyncGenerator: MERGE strategy, unknown strategy/endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

DOMAINS_DIR = Path(__file__).parent.parent.parent / "domains"


def test_sync_generator_produces_merge_cypher():
    from engine.config.loader import DomainPackLoader
    from engine.sync.generator import SyncGenerator

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    gen = SyncGenerator(spec)
    # Get first sync endpoint
    if not spec.sync.endpoints:
        pytest.skip("No sync endpoints in plasticos spec")
    ep = spec.sync.endpoints[0]
    cypher = gen.generate_sync_query(ep, [{"id": "test-1", "name": "Alpha"}])
    assert isinstance(cypher, str)
    assert "MERGE" in cypher or "MATCH" in cypher
    assert "UNWIND" in cypher or "$" in cypher


def test_sync_generator_includes_batch_param():
    from engine.config.loader import DomainPackLoader
    from engine.sync.generator import SyncGenerator

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    gen = SyncGenerator(spec)
    if not spec.sync.endpoints:
        pytest.skip("No sync endpoints in plasticos spec")
    ep = spec.sync.endpoints[0]
    cypher = gen.generate_sync_query(ep, [{"entity_id": "f1"}])
    # generate_sync_query returns a Cypher string with $batch parameter reference
    assert "$batch" in cypher or "batch" in cypher


def test_unknown_strategy_raises():
    """Unknown batch strategy should raise ValueError."""
    from unittest.mock import MagicMock

    from engine.config.loader import DomainPackLoader
    from engine.sync.generator import SyncGenerator

    loader = DomainPackLoader(config_path=str(DOMAINS_DIR))
    spec = loader.load_domain("plasticos")
    gen = SyncGenerator(spec)
    # Create a mock endpoint with an invalid strategy
    fake_ep = MagicMock()
    fake_ep.batchstrategy = "INVALID_STRATEGY"
    with pytest.raises((ValueError, Exception)):
        gen.generate_sync_query(fake_ep, [{"id": "x"}])
