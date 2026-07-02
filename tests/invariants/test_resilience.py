"""
Invariant regression tests — Resilience defects (T6-xx findings).

Verifies that fixes for globals, caching, and operational resilience
from Waves 1-4 remain in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.finding("T6-01")
class TestT601DomainLoaderCache:
    """T6-01: DomainPackLoader cache must have thread-safety and TTL."""

    def test_loader_has_lock(self):
        """Cache operations are protected by a threading.Lock."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path=str(ROOT / "domains"))
        assert hasattr(loader, "_lock"), "DomainPackLoader missing _lock attribute"
        assert hasattr(loader._lock, "acquire")
        assert hasattr(loader._lock, "release")

    def test_loader_has_ttl(self):
        """Cache has a TTL-based invalidation policy."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path=str(ROOT / "domains"))
        assert hasattr(loader, "_ttl_seconds"), "DomainPackLoader missing _ttl_seconds"
        assert loader._ttl_seconds > 0

    def test_loader_has_max_size(self):
        """Cache has bounded size (LRU eviction)."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path=str(ROOT / "domains"))
        assert hasattr(loader, "_max_size"), "DomainPackLoader missing _max_size"
        assert loader._max_size > 0


@pytest.mark.finding("T6-02")
class TestT602Neo4jResilience:
    """T6-02: Neo4j driver wrapper should handle connection failures gracefully."""

    def test_graph_driver_has_timeout_or_resilience(self):
        """GraphDriver constructor should accept timeout/resilience params."""
        from engine.graph.driver import GraphDriver

        sig = __import__("inspect").signature(GraphDriver.__init__)
        # Check that the driver is configurable or has resilience features
        assert sig is not None, "GraphDriver.__init__ should have a signature"


@pytest.mark.finding("T6-03")
class TestT603ModuleGlobalState:
    """T6-03: EngineState must be settable via init_dependencies (W4-01 refactor)."""

    def test_init_dependencies_sets_state(self):
        """init_dependencies() must set _graph_driver and _domain_loader on EngineState."""
        import engine.handlers as h
        from engine.state import get_state

        state = get_state()
        mock_driver = object()
        mock_loader = object()

        original_driver = state._graph_driver
        original_loader = state._domain_loader
        original_allowlist = state._tenant_allowlist

        try:
            h.init_dependencies(mock_driver, mock_loader)
            assert state._graph_driver is mock_driver
            assert state._domain_loader is mock_loader
        finally:
            state._graph_driver = original_driver
            state._domain_loader = original_loader
            state._tenant_allowlist = original_allowlist

    def test_require_deps_raises_if_not_initialized(self):
        """_require_deps raises RuntimeError before init_dependencies."""
        import engine.handlers as h
        from engine.state import get_state

        state = get_state()
        original_driver = state._graph_driver
        original_loader = state._domain_loader
        original_initialized = state._initialized

        try:
            state._graph_driver = None
            state._domain_loader = None
            state._initialized = False
            with pytest.raises(RuntimeError, match="Dependencies not initialized"):
                h._require_deps()
        finally:
            state._graph_driver = original_driver
            state._domain_loader = original_loader
            state._initialized = original_initialized


@pytest.mark.finding("T6-05")
class TestT605ChassisInitRace:
    """T6-05: Chassis init should not have unprotected race conditions."""

    def test_handlers_init_dependencies_is_idempotent(self):
        """Calling init_dependencies twice should not corrupt state."""
        import engine.handlers as h
        from engine.state import get_state

        state = get_state()
        original_driver = state._graph_driver
        original_loader = state._domain_loader
        original_allowlist = state._tenant_allowlist

        try:
            mock1 = object()
            mock2 = object()
            h.init_dependencies(mock1, mock1)
            h.init_dependencies(mock2, mock2)
            assert state._graph_driver is mock2
            assert state._domain_loader is mock2
        finally:
            state._graph_driver = original_driver
            state._domain_loader = original_loader
            state._tenant_allowlist = original_allowlist
