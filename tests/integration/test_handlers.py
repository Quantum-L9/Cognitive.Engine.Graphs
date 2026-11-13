"""Integration tests — action handlers: sync, match, admin.

These tests use mocked Neo4j (AsyncMock) and a mocked DomainPackLoader
so they run without a live database while still exercising the real handler
dispatch, validation, and response-shape logic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.schema import NodeSpec, PropertySpec
from engine.graph.driver import GraphDriver
from engine.handlers import handle_admin, handle_match, handle_sync, init_dependencies
from engine.state import get_state

pytestmark = pytest.mark.unit

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_mock_driver() -> MagicMock:
    """GraphDriver stub: execute_query returns an empty list by default."""
    driver = MagicMock(spec=GraphDriver)
    driver.execute_query = AsyncMock(return_value=[])
    return driver


def _make_mock_spec(*, tenant: str = "tenant-a") -> MagicMock:
    """Minimal DomainSpec mock sufficient for handler dispatch."""
    spec = MagicMock()
    spec.domain.id = tenant
    spec.capabilities = []
    spec.causal.enabled = False
    spec.compliance = MagicMock()
    spec.compliance.enabled = False
    spec.compliance.flush_audit = AsyncMock()
    spec.feedbackloop.enabled = False
    spec.counterfactual.enabled = False
    spec.semantic_registry.enabled = False
    spec.decision_arbitration.enabled = False
    return spec


def _reset_state() -> None:
    """Reset the EngineState singleton between tests."""
    state = get_state()
    state._initialized = False
    state._graph_driver = None
    state._domain_loader = None
    state._tenant_allowlist = None
    state.compliance_engines.clear()


# ── handle_sync ───────────────────────────────────────────────────────────────


def test_handle_sync_rejects_missing_entity_type() -> None:
    """handle_sync must raise when entity_type is absent from payload."""
    from engine.handlers import ValidationError

    loader = MagicMock(spec=DomainPackLoader)
    loader.load_domain.return_value = _make_mock_spec()
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    coro = handle_sync("tenant-a", {"batch": [{"entity_id": "x"}]})
    with pytest.raises((ValidationError, Exception)):
        asyncio.run(coro)


def test_handle_sync_rejects_empty_batch() -> None:
    """handle_sync must raise when batch is an empty list."""
    from engine.handlers import ValidationError

    loader = MagicMock(spec=DomainPackLoader)
    loader.load_domain.return_value = _make_mock_spec()
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    coro = handle_sync("tenant-a", {"entity_type": "Buyer", "batch": []})
    with pytest.raises((ValidationError, Exception)):
        asyncio.run(coro)


def test_handle_sync_returns_success_shape() -> None:
    """handle_sync returns status/entity_type/synced_count on a successful run."""
    spec = _make_mock_spec()
    endpoint = MagicMock()
    endpoint.path = "/sync/Buyer"
    endpoint.targetedge = None  # a node endpoint, as SyncEndpointSpec defaults it
    endpoint.refnamespace = None  # no graph-ref namespace declared, as SyncEndpointSpec defaults it
    # The node it writes, declared with its types: sync validates rows against
    # the spec before anything runs (typed sync contract).
    endpoint.targetnode = "Buyer"
    endpoint.idproperty = "entity_id"
    endpoint.taxonomyedges = []
    endpoint.childsync = []
    endpoint.fieldsupdated = None
    spec.ontology.nodes = [
        NodeSpec(label="Buyer", properties=[PropertySpec(name="entity_id", type="string", required=True)])
    ]
    spec.sync = MagicMock()
    spec.sync.endpoints = [endpoint]

    loader = MagicMock(spec=DomainPackLoader)
    loader.load_domain.return_value = spec
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    with patch("engine.handlers.SyncGenerator") as mock_gen_cls:
        mock_gen_cls.return_value.generate_sync_query.return_value = "MERGE (n:Buyer {entity_id: $entity_id})"
        mock_gen_cls.return_value.project_batch.return_value = ([{"node": {"entity_id": "buyer-1"}}], {})
        result = asyncio.run(
            handle_sync(
                "tenant-a",
                {"entity_type": "Buyer", "batch": [{"entity_id": "buyer-1"}]},
            )
        )

    assert result["status"] == "success"
    assert result["entity_type"] == "Buyer"
    assert result["synced_count"] == 1


# ── handle_match ──────────────────────────────────────────────────────────────


def test_handle_match_rejects_missing_query() -> None:
    """handle_match must raise when 'query' key is absent."""
    from engine.handlers import ValidationError

    loader = MagicMock(spec=DomainPackLoader)
    loader.load_domain.return_value = _make_mock_spec()
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    coro = handle_match("tenant-a", {"match_direction": "a_to_b"})
    with pytest.raises((ValidationError, Exception)):
        asyncio.run(coro)


def test_handle_match_rejects_missing_direction() -> None:
    """handle_match must raise when 'match_direction' key is absent."""
    from engine.handlers import ValidationError

    loader = MagicMock(spec=DomainPackLoader)
    loader.load_domain.return_value = _make_mock_spec()
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    coro = handle_match("tenant-a", {"query": {"revenue": 0.8}})
    with pytest.raises((ValidationError, Exception)):
        asyncio.run(coro)


# ── handle_admin ──────────────────────────────────────────────────────────────


def test_handle_admin_list_domains() -> None:
    """handle_admin subaction=list_domains returns the domain list."""
    loader = MagicMock(spec=DomainPackLoader)
    loader.list_domains.return_value = ["orbital-logistics", "cheese-aging"]
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    result = asyncio.run(handle_admin("tenant-a", {"subaction": "list_domains"}))

    assert result["domains"] == ["orbital-logistics", "cheese-aging"]


def test_handle_admin_rejects_unknown_subaction() -> None:
    """handle_admin must raise on an unrecognized subaction."""
    from engine.handlers import ValidationError

    loader = MagicMock(spec=DomainPackLoader)
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    coro = handle_admin("tenant-a", {"subaction": "does_not_exist"})
    with pytest.raises((ValidationError, Exception)):
        asyncio.run(coro)


def test_handle_admin_rejects_missing_subaction() -> None:
    """handle_admin must raise when subaction key is absent."""
    from engine.handlers import ValidationError

    loader = MagicMock(spec=DomainPackLoader)
    driver = _make_mock_driver()
    _reset_state()
    init_dependencies(graph_driver=driver, domain_loader=loader)

    coro = handle_admin("tenant-a", {})
    with pytest.raises((ValidationError, Exception)):
        asyncio.run(coro)
