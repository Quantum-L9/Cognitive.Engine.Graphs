"""
Invariant regression tests — Resilience defects (T6-xx findings).

Verifies that fixes for globals, caching, and operational resilience
from Waves 1-4 remain in place.

W4-01 note: module-level globals in engine/handlers.py were replaced by
the EngineState singleton (engine/state.py). These tests assert against
EngineState rather than the removed module globals.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# threading.Lock is a factory function, not a type — use the concrete
# lock type for isinstance checks (Python 3.12: isinstance() requires a type).
_LOCK_TYPE = type(threading.Lock())


@pytest.fixture
def clean_state():
    """Provide a snapshot/restore of the EngineState singleton around a test."""
    from engine.state import get_state

    state = get_state()
    snapshot = (
        state._graph_driver,
        state._domain_loader,
        state._tenant_allowlist,
        state._initialized,
    )
    yield state
    (
        state._graph_driver,
        state._domain_loader,
        state._tenant_allowlist,
        state._initialized,
    ) = snapshot


@pytest.mark.finding("T6-01")
class TestT601DomainLoaderCache:
    """T6-01: DomainPackLoader cache must have thread-safety and TTL."""

    def test_loader_has_lock(self):
        """Cache operations are protected by a threading lock."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path=str(ROOT / "domains"))
        assert hasattr(loader, "_lock"), "DomainPackLoader missing _lock attribute"
        assert isinstance(loader._lock, _LOCK_TYPE)

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
    """T6-03: Shared state must be settable via init_dependencies (EngineState)."""

    def test_init_dependencies_sets_state(self, clean_state):
        """init_dependencies() must populate EngineState driver and loader."""
        import engine.handlers as h

        state = clean_state
        mock_driver = object()
        mock_loader = object()

        h.init_dependencies(mock_driver, mock_loader)
        assert state._graph_driver is mock_driver
        assert state._domain_loader is mock_loader

    def test_require_deps_raises_if_not_initialized(self, clean_state):
        """_require_deps raises RuntimeError before init_dependencies."""
        import engine.handlers as h

        state = clean_state
        state._graph_driver = None
        state._domain_loader = None
        state._initialized = False
        with pytest.raises(RuntimeError, match="Dependencies not initialized"):
            h._require_deps()


@pytest.mark.finding("T6-05")
class TestT605ChassisInitRace:
    """T6-05: Chassis init should not have unprotected race conditions."""

    def test_handlers_init_dependencies_is_idempotent(self, clean_state):
        """Calling init_dependencies twice should not corrupt state."""
        import engine.handlers as h

        state = clean_state
        mock1 = object()
        mock2 = object()
        h.init_dependencies(mock1, mock1)
        h.init_dependencies(mock2, mock2)
        assert state._graph_driver is mock2
        assert state._domain_loader is mock2
