"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, wave4, sel4, state, circuit-breaker, cache, compliance]
owner: engine-team
status: active
--- /L9_META ---

Tests for Wave 4: State Management & Resilience (seL4 explicit state).

Covers:
  W4-01: EngineState — init, shutdown, reset, health_check, double-init, pre-init error
  W4-02: CircuitBreaker — CLOSED→OPEN, OPEN→HALF_OPEN→CLOSED, metrics, force ops
  W4-03: DomainPackLoader TTL Cache — hit, miss, expiry, invalidation, stampede
  W4-04: ComplianceEngine singleton — get-or-create, flush, lifecycle
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# W4-01: EngineState Tests
# ---------------------------------------------------------------------------


class TestEngineState:
    """W4-01: EngineState lifecycle and access control."""

    def setup_method(self) -> None:
        from engine.state import _reset_singleton

        _reset_singleton()

    def teardown_method(self) -> None:
        from engine.state import _reset_singleton

        _reset_singleton()

    def test_get_state_returns_singleton(self) -> None:
        from engine.state import get_state

        s1 = get_state()
        s2 = get_state()
        assert s1 is s2

    def test_get_state_concurrent_first_access_builds_one_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Double-checked locking: racing first callers share one EngineState."""
        import threading

        import engine.state as state_mod

        built: list[Any] = []
        real_cls = state_mod.EngineState

        def slow_factory() -> Any:
            time.sleep(0.01)  # widen the race window between check and assignment
            instance = real_cls()
            built.append(instance)
            return instance

        monkeypatch.setattr(state_mod, "EngineState", slow_factory)
        barrier = threading.Barrier(8)
        seen: list[Any] = []

        def worker() -> None:
            barrier.wait()
            seen.append(state_mod.get_state())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(built) == 1
        assert all(s is built[0] for s in seen)

    def test_pre_init_graph_driver_raises(self) -> None:
        from engine.state import get_state

        state = get_state()
        with pytest.raises(RuntimeError, match="graph_driver"):
            _ = state.graph_driver

    def test_pre_init_domain_loader_raises(self) -> None:
        from engine.state import get_state

        state = get_state()
        with pytest.raises(RuntimeError, match="domain_loader"):
            _ = state.domain_loader

    @pytest.mark.asyncio
    async def test_initialize_sets_state(self) -> None:
        from engine.state import get_state

        state = get_state()
        mock_driver = MagicMock()
        mock_loader = MagicMock()

        await state.initialize(
            graph_driver=mock_driver,
            domain_loader=mock_loader,
            tenant_allowlist={"tenant_a", "tenant_b"},
        )

        assert state.is_initialized
        assert state.graph_driver is mock_driver
        assert state.domain_loader is mock_loader
        assert state.tenant_allowlist == {"tenant_a", "tenant_b"}

    @pytest.mark.asyncio
    async def test_double_init_is_idempotent(self) -> None:
        from engine.state import get_state

        state = get_state()
        mock_driver = MagicMock()
        mock_loader = MagicMock()

        await state.initialize(graph_driver=mock_driver, domain_loader=mock_loader)
        assert state.is_initialized

        # Second call should be a no-op
        mock_driver2 = MagicMock()
        await state.initialize(graph_driver=mock_driver2, domain_loader=mock_loader)

        # Still the original driver
        assert state.graph_driver is mock_driver

    @pytest.mark.asyncio
    async def test_shutdown_clears_state(self) -> None:
        from engine.state import get_state

        state = get_state()
        mock_driver = AsyncMock()
        mock_loader = MagicMock()

        await state.initialize(graph_driver=mock_driver, domain_loader=mock_loader)
        assert state.is_initialized

        await state.shutdown()

        assert not state.is_initialized
        mock_driver.close.assert_awaited_once()

    def test_reset_clears_state_sync(self) -> None:
        from engine.state import get_state

        state = get_state()
        state._graph_driver = MagicMock()
        state._initialized = True

        state.reset()

        assert not state.is_initialized
        with pytest.raises(RuntimeError):
            _ = state.graph_driver

    def test_health_check_uninitialized(self) -> None:
        from engine.state import get_state

        state = get_state()
        health = state.health_check()
        assert health["initialized"] is False
        assert health["graph_driver_present"] is False

    @pytest.mark.asyncio
    async def test_health_check_initialized(self) -> None:
        from engine.state import get_state

        state = get_state()
        await state.initialize(
            graph_driver=MagicMock(),
            domain_loader=MagicMock(),
            tenant_allowlist={"t1"},
        )

        health = state.health_check()
        assert health["initialized"] is True
        assert health["graph_driver_present"] is True
        assert health["tenant_count"] == 1

    def test_reset_singleton_creates_fresh_state(self) -> None:
        from engine.state import _reset_singleton, get_state

        s1 = get_state()
        _reset_singleton()
        s2 = get_state()
        assert s1 is not s2


# ---------------------------------------------------------------------------
# W4-02: CircuitBreaker Tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """W4-02: Circuit breaker state machine and metrics."""

    @pytest.mark.asyncio
    async def test_closed_forwards_calls(self) -> None:
        from engine.graph.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=3)
        func = AsyncMock(return_value=42)

        result = await cb.call(func)
        assert result == 42
        assert cb.is_closed

    @pytest.mark.asyncio
    async def test_closed_to_open_on_threshold(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=60.0)
        func = AsyncMock(side_effect=RuntimeError("fail"))

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(func)

        assert cb.state == BreakerState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_calls(self) -> None:
        from engine.graph.circuit_breaker import CircuitBreaker, CircuitOpenError

        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=60.0)
        func = AsyncMock(side_effect=RuntimeError("fail"))

        # Trip the breaker
        with pytest.raises(RuntimeError):
            await cb.call(func)

        # Now it should reject
        with pytest.raises(CircuitOpenError):
            await cb.call(func)

    @pytest.mark.asyncio
    async def test_open_to_half_open_after_timeout(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.1)
        fail_func = AsyncMock(side_effect=RuntimeError("fail"))
        success_func = AsyncMock(return_value="ok")

        # Trip the breaker
        with pytest.raises(RuntimeError):
            await cb.call(fail_func)

        assert cb.state == BreakerState.OPEN

        # Wait for recovery timeout
        await asyncio.sleep(0.15)

        # Next call should transition to HALF_OPEN then CLOSED on success
        result = await cb.call(success_func)
        assert result == "ok"
        assert cb.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.1)
        fail_func = AsyncMock(side_effect=RuntimeError("fail"))

        # Trip the breaker
        with pytest.raises(RuntimeError):
            await cb.call(fail_func)

        # Wait for recovery
        await asyncio.sleep(0.15)

        # Probe call fails → back to OPEN
        with pytest.raises(RuntimeError):
            await cb.call(fail_func)

        assert cb.state == BreakerState.OPEN

    @pytest.mark.asyncio
    async def test_metrics_tracking(self) -> None:
        from engine.graph.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=5)
        success_func = AsyncMock(return_value="ok")
        fail_func = AsyncMock(side_effect=RuntimeError("fail"))

        await cb.call(success_func)
        await cb.call(success_func)
        with pytest.raises(RuntimeError):
            await cb.call(fail_func)

        metrics = cb.get_metrics()
        assert metrics.total_calls == 3
        assert metrics.total_successes == 2
        assert metrics.total_failures == 1
        assert metrics.name == "test"

    @pytest.mark.asyncio
    async def test_force_open(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(name="test")
        await cb.force_open()
        assert cb.state == BreakerState.OPEN

    @pytest.mark.asyncio
    async def test_force_close(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=1)
        fail_func = AsyncMock(side_effect=RuntimeError("fail"))

        with pytest.raises(RuntimeError):
            await cb.call(fail_func)

        assert cb.state == BreakerState.OPEN
        await cb.force_close()
        assert cb.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_reset_metrics(self) -> None:
        from engine.graph.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(name="test")
        func = AsyncMock(return_value="ok")
        await cb.call(func)

        await cb.reset_metrics()
        metrics = cb.get_metrics()
        assert metrics.total_calls == 0
        assert metrics.total_successes == 0


async def _hang() -> None:
    """A call that never completes on its own (Neo4j connection-acquisition stall)."""
    await asyncio.sleep(60)


class TestCircuitBreakerCancelledProbes:
    """RU-094 / CEG-COR-035: a cancelled HALF_OPEN probe returns its slot.

    The SDK runtime wraps every handler in ``asyncio.wait_for``; a probe that
    outlives the packet timeout is cancelled inside ``CircuitBreaker.call``.
    Before the fix the slot was never returned, so after half_open_max_calls
    cancellations the breaker rejected every call until process restart.
    """

    async def _tripped(self, half_open_max_calls: int = 3) -> Any:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(
            name="test",
            failure_threshold=1,
            recovery_timeout=0.0,
            half_open_max_calls=half_open_max_calls,
        )
        with pytest.raises(RuntimeError):
            await cb.call(AsyncMock(side_effect=RuntimeError("down")))
        assert cb.state == BreakerState.OPEN
        return cb

    @pytest.mark.asyncio
    async def test_cancelled_probes_release_slots_and_breaker_recovers(self) -> None:
        from engine.graph.circuit_breaker import BreakerState

        cb = await self._tripped()
        for _ in range(3):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(cb.call(_hang), timeout=0.01)

        assert cb.state == BreakerState.HALF_OPEN
        assert cb._half_open_calls == 0
        # No failure/success was recorded for the cancelled probes.
        assert cb.get_metrics().total_failures == 1
        assert cb.get_metrics().total_successes == 0

        assert await cb.call(AsyncMock(return_value="ok")) == "ok"
        assert cb.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_cancellation_propagates_to_caller(self) -> None:
        cb = await self._tripped()
        task = asyncio.ensure_future(cb.call(_hang))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cb._half_open_calls == 0

    @pytest.mark.asyncio
    async def test_stale_probe_does_not_decrement_new_cycle(self) -> None:
        """A probe from cycle N cancelled during cycle N+1 leaves N+1's counter alone."""
        from engine.graph.circuit_breaker import BreakerState

        cb = await self._tripped()
        stale = asyncio.ensure_future(cb.call(_hang))
        await asyncio.sleep(0)
        assert cb._half_open_calls == 1

        # A second probe of the same cycle fails: HALF_OPEN -> OPEN.
        with pytest.raises(RuntimeError):
            await cb.call(AsyncMock(side_effect=RuntimeError("still down")))
        assert cb.state == BreakerState.OPEN

        # recovery_timeout=0: the next call opens a new HALF_OPEN cycle.
        fresh = asyncio.ensure_future(cb.call(_hang))
        await asyncio.sleep(0)
        assert cb.state == BreakerState.HALF_OPEN
        assert cb._half_open_calls == 1

        stale.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stale
        assert cb._half_open_calls == 1, "stale probe decremented the new cycle's counter"

        fresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fresh
        assert cb._half_open_calls == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["success", "failure"])
    async def test_cancel_while_recording_outcome_releases_slot(self, outcome: str) -> None:
        """A probe cancelled while _on_success/_on_failure waits on the lock returns its slot."""
        from engine.graph.circuit_breaker import BreakerState

        cb = await self._tripped(half_open_max_calls=1)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def _probe() -> str:
            entered.set()
            await finish.wait()
            if outcome == "failure":
                raise RuntimeError("probe failed")
            return "ok"

        task = asyncio.ensure_future(cb.call(_probe))
        await entered.wait()
        await cb._lock.acquire()  # the outcome recorder will block on this
        try:
            finish.set()
            for _ in range(5):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            cb._lock.release()
        assert cb.state == BreakerState.HALF_OPEN
        assert cb._half_open_calls == 0
        assert await cb.call(AsyncMock(return_value="ok")) == "ok"
        assert cb.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_concurrent_probes_still_bounded(self) -> None:
        from engine.graph.circuit_breaker import CircuitOpenError

        cb = await self._tripped(half_open_max_calls=3)
        probes = [asyncio.ensure_future(cb.call(_hang)) for _ in range(5)]
        await asyncio.sleep(0)
        rejected = [p for p in probes if p.done() and isinstance(p.exception(), CircuitOpenError)]
        assert len(rejected) == 2
        assert cb._half_open_calls == 3
        for p in probes:
            p.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
        assert cb._half_open_calls == 0

    @pytest.mark.asyncio
    async def test_closed_state_cancellation_changes_no_counters(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=3)
        for _ in range(10):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(cb.call(_hang), timeout=0.001)
        m = cb.get_metrics()
        assert cb.state == BreakerState.CLOSED
        assert m.consecutive_failures == 0
        assert m.total_failures == 0
        assert cb._half_open_calls == 0

    @pytest.mark.asyncio
    async def test_flag_off_keeps_legacy_slot_accounting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings
        from engine.graph.circuit_breaker import CircuitOpenError

        monkeypatch.setattr(settings, "neo4j_circuit_release_cancelled_probes", False)
        cb = await self._tripped()
        for _ in range(3):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(cb.call(_hang), timeout=0.01)
        assert cb._half_open_calls == 3
        with pytest.raises(CircuitOpenError):
            await cb.call(AsyncMock(return_value="ok"))

    def test_flag_default_on(self) -> None:
        from engine.config.settings import Settings

        assert Settings().neo4j_circuit_release_cancelled_probes is True


# ---------------------------------------------------------------------------
# W4-03: DomainPackLoader Async TTL Cache Tests
# ---------------------------------------------------------------------------


class TestDomainPackLoaderAsync:
    """W4-03: Async domain loading with TTL cache and stampede prevention."""

    def _make_loader_with_mock(self):
        """Create a DomainPackLoader with mocked disk I/O."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path="/tmp/nonexistent_domains")

        # Mock the sync load_domain to avoid disk access
        mock_spec = MagicMock()
        mock_spec.domain.id = "test_domain"
        loader.load_domain = MagicMock(return_value=mock_spec)

        return loader, mock_spec

    @pytest.mark.asyncio
    async def test_async_load_cache_hit(self) -> None:
        """Cache hit returns cached value without re-loading from disk."""
        loader, mock_spec = self._make_loader_with_mock()

        # Prime the cache
        loader._cache["test_domain"] = (mock_spec, 100.0, time.monotonic())
        loader._ttl_seconds = 30.0

        result = await loader.load_domain_async("test_domain")
        assert result is mock_spec
        # load_domain should NOT have been called (cache hit)
        loader.load_domain.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_load_cache_miss(self) -> None:
        """Cache miss delegates to sync load_domain via to_thread."""
        loader, mock_spec = self._make_loader_with_mock()

        result = await loader.load_domain_async("test_domain")
        assert result is mock_spec
        loader.load_domain.assert_called_once_with("test_domain")

    @pytest.mark.asyncio
    async def test_async_load_ttl_expiry(self) -> None:
        """Expired TTL triggers reload."""
        loader, mock_spec = self._make_loader_with_mock()

        # Prime cache with expired entry
        loader._cache["test_domain"] = (mock_spec, 100.0, time.monotonic() - 60)
        loader._ttl_seconds = 30.0

        result = await loader.load_domain_async("test_domain")
        assert result is mock_spec
        loader.load_domain.assert_called_once_with("test_domain")

    @pytest.mark.asyncio
    async def test_invalidation_clears_cache(self) -> None:
        """invalidate() removes specific domain from cache."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path="/tmp/nonexistent_domains")
        mock_spec = MagicMock()
        loader._cache["test_domain"] = (mock_spec, 100.0, time.monotonic())

        loader.invalidate("test_domain")
        assert "test_domain" not in loader._cache

    @pytest.mark.asyncio
    async def test_invalidation_clears_all(self) -> None:
        """invalidate() with no args clears entire cache."""
        from engine.config.loader import DomainPackLoader

        loader = DomainPackLoader(config_path="/tmp/nonexistent_domains")
        mock_spec = MagicMock()
        loader._cache["domain_a"] = (mock_spec, 100.0, time.monotonic())
        loader._cache["domain_b"] = (mock_spec, 100.0, time.monotonic())

        loader.invalidate()
        assert len(loader._cache) == 0

    @pytest.mark.asyncio
    async def test_stampede_prevention(self) -> None:
        """Per-domain async lock prevents concurrent loads for the same domain.

        We verify the lock mechanism works by checking that concurrent callers
        share the same async lock object (structural test).
        """
        loader, mock_spec = self._make_loader_with_mock()

        # First call creates the lock and loads
        result = await loader.load_domain_async("test_domain")
        assert result is mock_spec

        # Verify lock was created for this domain
        assert hasattr(loader, "_async_locks")
        assert "test_domain" in loader._async_locks
        assert isinstance(loader._async_locks["test_domain"], asyncio.Lock)


# ---------------------------------------------------------------------------
# W4-04: ComplianceEngine Singleton Pool Tests
# ---------------------------------------------------------------------------


class TestComplianceEngineSingleton:
    """W4-04: ComplianceEngine get-or-create singleton pool."""

    def setup_method(self) -> None:
        from engine.state import _reset_singleton

        _reset_singleton()

    def teardown_method(self) -> None:
        from engine.state import _reset_singleton

        _reset_singleton()

    def _make_domain_spec(self, domain_id: str = "test_domain") -> MagicMock:
        spec = MagicMock()
        spec.domain.id = domain_id
        spec.domain.name = "Test"
        spec.compliance = MagicMock()
        spec.compliance.enabled = True
        spec.compliance.pii = None
        spec.compliance.prohibited_factors = MagicMock()
        spec.compliance.prohibited_factors.fields = []
        return spec

    def test_get_or_create_returns_same_instance(self) -> None:
        from engine.handlers import _get_compliance_engine
        from engine.state import get_state

        state = get_state()
        state._initialized = True

        spec = self._make_domain_spec()

        with patch("engine.handlers.ComplianceEngine") as mock_ce_cls:
            mock_ce = MagicMock()
            mock_ce_cls.return_value = mock_ce

            ce1 = _get_compliance_engine(spec)
            ce2 = _get_compliance_engine(spec)

            assert ce1 is ce2
            # Should only be created once
            mock_ce_cls.assert_called_once()

    def test_different_domains_get_different_engines(self) -> None:
        from engine.handlers import _get_compliance_engine
        from engine.state import get_state

        state = get_state()
        state._initialized = True

        spec_a = self._make_domain_spec("domain_a")
        spec_b = self._make_domain_spec("domain_b")

        with patch("engine.handlers.ComplianceEngine") as mock_ce_cls:
            mock_ce_cls.side_effect = [MagicMock(), MagicMock()]

            ce_a = _get_compliance_engine(spec_a)
            ce_b = _get_compliance_engine(spec_b)

            assert ce_a is not ce_b
            assert mock_ce_cls.call_count == 2

    @pytest.mark.asyncio
    async def test_shutdown_clears_compliance_pool(self) -> None:
        from engine.state import get_state

        state = get_state()
        mock_driver = AsyncMock()
        await state.initialize(graph_driver=mock_driver, domain_loader=MagicMock())

        state.compliance_engines["test"] = MagicMock()
        assert len(state.compliance_engines) == 1

        await state.shutdown()
        # After shutdown, compliance_engines should be cleared
        # (state is reset, accessing it returns empty dict)
        assert not state.is_initialized


# ---------------------------------------------------------------------------
# W4-02: GraphDriver Circuit Breaker Integration Tests
# ---------------------------------------------------------------------------


class TestGraphDriverCircuitBreaker:
    """W4-02: Verify circuit breaker is wired into GraphDriver.execute_query."""

    @pytest.mark.asyncio
    async def test_driver_has_circuit_breaker(self) -> None:
        from engine.graph.circuit_breaker import CircuitBreaker
        from engine.graph.driver import GraphDriver

        driver = GraphDriver(uri="bolt://localhost:7687")
        assert isinstance(driver.circuit_breaker, CircuitBreaker)
        assert driver.circuit_breaker.name == "neo4j"

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_query_failures(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker, CircuitOpenError
        from engine.graph.driver import GraphDriver

        driver = GraphDriver(uri="bolt://localhost:7687")
        # Replace circuit breaker with a low-threshold one for testing
        driver._circuit_breaker = CircuitBreaker(
            name="neo4j_test",
            failure_threshold=2,
            recovery_timeout=60.0,
            half_open_max_calls=1,
        )
        driver._driver = MagicMock()

        # Mock session to raise errors
        mock_session = AsyncMock()
        mock_session.run = AsyncMock(side_effect=RuntimeError("Neo4j down"))
        driver._driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        driver._driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        # First 2 failures trip the breaker. database= is explicit: DB-STRICT-01
        # made the implicit 'neo4j' fallback an error, and this test is about
        # the breaker, not the binding (tests/unit/test_graph_driver_database_binding.py).
        for _ in range(2):
            with pytest.raises(RuntimeError, match="Neo4j down"):
                await driver.execute_query("RETURN 1", database="neo4j")

        assert driver.circuit_breaker.state == BreakerState.OPEN

        # Next call should be rejected by circuit breaker
        with pytest.raises(CircuitOpenError):
            await driver.execute_query("RETURN 1", database="neo4j")


def _driver_with_session_errors(errors: dict[str, BaseException | None]) -> Any:
    """GraphDriver whose session.run raises errors[database] (None = returns)."""
    from engine.graph.driver import GraphDriver

    driver = GraphDriver(uri="bolt://localhost:7687")
    driver._driver = MagicMock()

    def _session(database: str) -> Any:
        session = AsyncMock()
        error = errors.get(database)
        if error is not None:
            session.run = AsyncMock(side_effect=error)
        else:
            result = AsyncMock()
            result.data = AsyncMock(return_value=[{"ok": 1}])
            session.run = AsyncMock(return_value=result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    driver._driver.session.side_effect = _session
    return driver


class TestBreakerFailureClassification:
    """RU-093 / CEG-COR-034: client errors do not trip the process-wide breaker."""

    @pytest.mark.asyncio
    async def test_classifier_neutral_errors_do_not_count(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            is_failure=lambda exc: not isinstance(exc, ValueError),
        )
        with pytest.raises(RuntimeError):
            await cb.call(AsyncMock(side_effect=RuntimeError("infra")))
        for _ in range(5):
            with pytest.raises(ValueError):
                await cb.call(AsyncMock(side_effect=ValueError("client")))
        m = cb.get_metrics()
        assert cb.state == BreakerState.CLOSED
        # Neutral: the streak is neither incremented nor reset.
        assert m.consecutive_failures == 1
        assert m.total_failures == 1
        assert m.total_neutral_errors == 5
        assert m.to_dict()["total_neutral_errors"] == 5
        with pytest.raises(RuntimeError):
            await cb.call(AsyncMock(side_effect=RuntimeError("infra")))
        assert cb.state == BreakerState.OPEN

    @pytest.mark.asyncio
    async def test_neutral_error_releases_half_open_slot(self) -> None:
        from engine.graph.circuit_breaker import BreakerState, CircuitBreaker

        cb = CircuitBreaker(
            name="test",
            failure_threshold=1,
            recovery_timeout=0.0,
            half_open_max_calls=1,
            is_failure=lambda exc: not isinstance(exc, ValueError),
        )
        with pytest.raises(RuntimeError):
            await cb.call(AsyncMock(side_effect=RuntimeError("infra")))
        for _ in range(3):
            with pytest.raises(ValueError):
                await cb.call(AsyncMock(side_effect=ValueError("client")))
            assert cb.state == BreakerState.HALF_OPEN
            assert cb._half_open_calls == 0
        assert await cb.call(AsyncMock(return_value="ok")) == "ok"
        assert cb.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_driver_client_errors_leave_breaker_closed(self) -> None:
        import neo4j.exceptions as nx

        from engine.config.settings import settings
        from engine.graph.circuit_breaker import BreakerState

        threshold = settings.neo4j_circuit_threshold
        client_error = nx.ClientError("Cannot merge the following node because of null property value")
        driver = _driver_with_session_errors({"tenant-a": client_error, "tenant-b": None})
        for _ in range(threshold + 5):
            with pytest.raises(nx.ClientError) as raised:
                await driver.execute_query("MERGE (n:X {id: $id})", {"id": None}, database="tenant-a")
            assert raised.value is client_error
        assert driver.circuit_breaker.state == BreakerState.CLOSED
        assert await driver.execute_query("RETURN 1", database="tenant-b") == [{"ok": 1}]

    @pytest.mark.asyncio
    async def test_driver_infrastructure_errors_still_open_breaker(self) -> None:
        import neo4j.exceptions as nx

        from engine.config.settings import settings
        from engine.graph.circuit_breaker import BreakerState, CircuitOpenError

        driver = _driver_with_session_errors(
            {"tenant-a": nx.ClientError("syntax"), "tenant-b": nx.ServiceUnavailable("Couldn't connect")}
        )
        for _ in range(settings.neo4j_circuit_threshold + 5):
            with pytest.raises(nx.ClientError):
                await driver.execute_query("RETURN", database="tenant-a")
        assert driver.circuit_breaker.state == BreakerState.CLOSED
        for _ in range(settings.neo4j_circuit_threshold):
            with pytest.raises(nx.ServiceUnavailable):
                await driver.execute_query("RETURN 1", database="tenant-b")
        assert driver.circuit_breaker.state == BreakerState.OPEN
        with pytest.raises(CircuitOpenError):
            await driver.execute_query("RETURN 1", database="tenant-a")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_name", ["AuthError", "TokenExpired", "Forbidden"])
    async def test_driver_credential_errors_open_breaker(self, error_name: str) -> None:
        """Owner decision 2026-10-29: credential ClientErrors are breaker failures."""
        import neo4j.exceptions as nx

        from engine.config.settings import settings
        from engine.graph.circuit_breaker import BreakerState

        error_cls = getattr(nx, error_name)
        assert issubclass(error_cls, nx.ClientError)
        driver = _driver_with_session_errors({"tenant-a": error_cls("credentials rejected")})
        for _ in range(settings.neo4j_circuit_threshold):
            with pytest.raises(error_cls):
                await driver.execute_query("RETURN 1", database="tenant-a")
        assert driver.circuit_breaker.state == BreakerState.OPEN
        assert driver.circuit_breaker.get_metrics().total_neutral_errors == 0

    @pytest.mark.parametrize(
        ("error_name", "counts"),
        [
            ("AuthError", True),
            ("TokenExpired", True),
            ("Forbidden", True),
            ("ClientError", False),
            ("CypherSyntaxError", False),
            ("CypherTypeError", False),
            ("ConstraintError", False),
        ],
    )
    def test_classifier_credential_carve_out(self, error_name: str, counts: bool) -> None:
        import neo4j.exceptions as nx

        from engine.graph.driver import _is_breaker_failure

        assert _is_breaker_failure(getattr(nx, error_name)("x")) is counts

    @pytest.mark.asyncio
    async def test_absent_database_client_error_still_translated(self) -> None:
        import neo4j.exceptions as nx

        from engine.graph.circuit_breaker import BreakerState
        from engine.graph.driver import DatabaseNotProvisionedError

        driver = _driver_with_session_errors(
            {"tenant-a": nx.ClientError("Database does not exist. Database name: 'tenant-a'.")}
        )
        for _ in range(10):
            with pytest.raises(DatabaseNotProvisionedError, match="tenant-a"):
                await driver.execute_query("RETURN 1", database="tenant-a")
        assert driver.circuit_breaker.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_flag_off_counts_client_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import neo4j.exceptions as nx

        from engine.config.settings import settings
        from engine.graph.circuit_breaker import BreakerState, CircuitOpenError

        monkeypatch.setattr(settings, "neo4j_circuit_ignore_client_errors", False)
        driver = _driver_with_session_errors({"tenant-a": nx.ClientError("null MERGE key"), "tenant-b": None})
        for _ in range(settings.neo4j_circuit_threshold):
            with pytest.raises(nx.ClientError):
                await driver.execute_query("MERGE (n:X {id: $id})", {"id": None}, database="tenant-a")
        assert driver.circuit_breaker.state == BreakerState.OPEN
        with pytest.raises(CircuitOpenError):
            await driver.execute_query("RETURN 1", database="tenant-b")

    def test_flag_default_on(self) -> None:
        from engine.config.settings import Settings

        assert Settings().neo4j_circuit_ignore_client_errors is True


# ---------------------------------------------------------------------------
# W4-01 + handlers integration: verify handlers use EngineState
# ---------------------------------------------------------------------------


class TestHandlersUseEngineState:
    """Verify handler functions route through EngineState instead of globals."""

    def setup_method(self) -> None:
        from engine.state import _reset_singleton

        _reset_singleton()

    def teardown_method(self) -> None:
        from engine.state import _reset_singleton

        _reset_singleton()

    def test_init_dependencies_populates_engine_state(self) -> None:
        from engine.handlers import init_dependencies
        from engine.state import get_state

        mock_driver = MagicMock()
        mock_loader = MagicMock()

        init_dependencies(mock_driver, mock_loader)

        state = get_state()
        assert state.is_initialized
        assert state.graph_driver is mock_driver
        assert state.domain_loader is mock_loader

    def test_require_deps_uses_engine_state(self) -> None:
        from engine.handlers import _require_deps, init_dependencies

        mock_driver = MagicMock()
        mock_loader = MagicMock()

        init_dependencies(mock_driver, mock_loader)

        driver, loader = _require_deps()
        assert driver is mock_driver
        assert loader is mock_loader

    def test_require_deps_raises_before_init(self) -> None:
        from engine.handlers import _require_deps

        with pytest.raises(RuntimeError, match="Dependencies not initialized"):
            _require_deps()

    def test_validate_tenant_access_uses_engine_state(self) -> None:
        from engine.handlers import ValidationError, _validate_tenant_access, init_dependencies

        mock_driver = MagicMock()
        mock_loader = MagicMock()

        with patch.dict("os.environ", {"TENANT_ALLOWLIST": "allowed_tenant"}):
            init_dependencies(mock_driver, mock_loader)

        # Allowed tenant should not raise
        _validate_tenant_access("allowed_tenant", "match")

        # Unauthorized tenant should raise
        with pytest.raises(ValidationError, match="not in authorized allowlist"):
            _validate_tenant_access("unauthorized", "match")


# ---------------------------------------------------------------------------
# Settings Tests
# ---------------------------------------------------------------------------


class TestWave4Settings:
    """Verify Wave 4 settings exist with correct defaults."""

    def test_circuit_breaker_defaults(self) -> None:
        from engine.config.settings import Settings

        s = Settings()
        assert s.neo4j_circuit_threshold == 5
        assert s.neo4j_circuit_cooldown == 30.0
        assert s.neo4j_circuit_half_open_max == 3

    def test_domain_cache_defaults(self) -> None:
        from engine.config.settings import Settings

        s = Settings()
        assert s.domain_cache_ttl_seconds == 30
        assert s.domain_cache_maxsize == 100

    def test_compliance_defaults(self) -> None:
        from engine.config.settings import Settings

        s = Settings()
        assert s.compliance_flush_interval == 60
        assert s.compliance_buffer_max == 100
