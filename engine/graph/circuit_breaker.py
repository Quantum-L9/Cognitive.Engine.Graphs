"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [graph]
tags: [circuit-breaker, resilience, sel4]
owner: engine-team
status: active
--- /L9_META ---

engine/graph/circuit_breaker.py — Circuit Breaker for Neo4j (seL4 W4-02)

Three-state circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED) that
protects against Neo4j outages. Wraps GraphDriver.execute_query() so that
after a configurable number of consecutive failures the breaker opens and
all further calls fail-fast with CircuitOpenError (503).

seL4 Analogue: seL4 proves every kernel operation terminates within
bounded steps. The circuit breaker gives CEG a bounded-execution analogue:
calls are either completed or rejected in O(1) when the circuit is open.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class BreakerState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit breaker is OPEN."""

    def __init__(self, breaker_name: str, retry_after: float) -> None:
        self.breaker_name = breaker_name
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker '{breaker_name}' is OPEN. Retry after {retry_after:.1f}s.")


# ---------------------------------------------------------------------------
# Metrics snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakerMetrics:
    """Immutable snapshot of circuit breaker metrics."""

    name: str
    state: str
    total_calls: int
    total_successes: int
    total_failures: int
    total_rejections: int
    state_changes: int
    consecutive_failures: int
    last_failure_time: float
    last_state_change_time: float
    # RU-093: exceptions the breaker's classifier ruled not to be failures
    # (GraphDriver: neo4j ClientError). Re-raised; never counted as failures.
    total_neutral_errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "total_calls": self.total_calls,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_rejections": self.total_rejections,
            "state_changes": self.state_changes,
            "consecutive_failures": self.consecutive_failures,
            "last_failure_time": self.last_failure_time,
            "last_state_change_time": self.last_state_change_time,
            "total_neutral_errors": self.total_neutral_errors,
        }


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


def _release_cancelled_probes() -> bool:
    """RU-094 flag: return a cancelled HALF_OPEN probe's slot."""
    from engine.config.settings import settings

    return settings.neo4j_circuit_release_cancelled_probes


class CircuitBreaker:
    """Async-aware circuit breaker with CLOSED / OPEN / HALF_OPEN state machine.

    Parameters
    ----------
    name:
        Human-readable name for logging and metrics.
    failure_threshold:
        Consecutive failures before CLOSED → OPEN.
    recovery_timeout:
        Seconds in OPEN before transitioning to HALF_OPEN.
    half_open_max_calls:
        Probe calls allowed in HALF_OPEN per recovery cycle.
    is_failure:
        Optional classifier. When given, an ``Exception`` for which it returns
        False is a neutral outcome: re-raised unchanged, not counted as a
        failure, does not reset the failure streak, and returns a HALF_OPEN
        probe slot. ``None`` (the default) counts every ``Exception`` (RU-093).
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        is_failure: Callable[[BaseException], bool] | None = None,
    ) -> None:
        self.name = name
        self._is_failure = is_failure
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        # State
        self._state: BreakerState = BreakerState.CLOSED
        self._consecutive_failures: int = 0
        self._half_open_calls: int = 0
        # RU-094: incremented on every transition into HALF_OPEN, so a probe
        # can tell whether the slot it took still belongs to the current cycle.
        self._half_open_cycle: int = 0
        self._opened_at: float = 0.0

        # Metrics
        self._total_calls: int = 0
        self._total_successes: int = 0
        self._total_failures: int = 0
        self._total_rejections: int = 0
        self._total_neutral_errors: int = 0
        self._state_changes: int = 0
        self._last_failure_time: float = 0.0
        self._last_state_change_time: float = 0.0

        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def is_closed(self) -> bool:
        return self._state == BreakerState.CLOSED

    @property
    def is_open(self) -> bool:
        return self._state == BreakerState.OPEN

    def get_metrics(self) -> BreakerMetrics:
        return BreakerMetrics(
            name=self.name,
            state=self._state.value,
            total_calls=self._total_calls,
            total_successes=self._total_successes,
            total_failures=self._total_failures,
            total_rejections=self._total_rejections,
            state_changes=self._state_changes,
            consecutive_failures=self._consecutive_failures,
            last_failure_time=self._last_failure_time,
            last_state_change_time=self._last_state_change_time,
            total_neutral_errors=self._total_neutral_errors,
        )

    # ------------------------------------------------------------------
    # Core call method
    # ------------------------------------------------------------------

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute func with circuit breaker protection.

        When OPEN and recovery_timeout has not elapsed, raises CircuitOpenError.
        When HALF_OPEN, allows limited probe calls.
        On success in HALF_OPEN, transitions to CLOSED.
        On failure, increments counter; trips breaker if threshold reached.
        A HALF_OPEN probe that ends without a result (``asyncio.CancelledError``,
        e.g. the SDK runtime's ``wait_for`` timeout) returns its slot and records
        neither success nor failure (RU-094).
        """
        probe_cycle: int | None = None
        async with self._lock:
            self._total_calls += 1

            # OPEN state check
            if self._state == BreakerState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.recovery_timeout:
                    self._transition_to(BreakerState.HALF_OPEN)
                else:
                    self._total_rejections += 1
                    raise CircuitOpenError(
                        self.name,
                        retry_after=self.recovery_timeout - elapsed,
                    )

            # HALF_OPEN: limit probe calls
            if self._state == BreakerState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    self._total_rejections += 1
                    raise CircuitOpenError(
                        self.name,
                        retry_after=self.recovery_timeout,
                    )
                self._half_open_calls += 1
                probe_cycle = self._half_open_cycle

        # Execute outside lock. The slot is released in ``finally`` whenever no
        # outcome was recorded: a cancellation during ``func`` *or* while
        # ``_on_success`` / ``_on_failure`` waits on the lock (RU-094).
        outcome_recorded = False
        try:
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                if self._is_failure is not None and not self._is_failure(exc):
                    self._on_neutral(probe_cycle)
                    outcome_recorded = True
                    raise
                await self._on_failure()
                outcome_recorded = True
                raise
            await self._on_success()
            outcome_recorded = True
            return result
        finally:
            if not outcome_recorded and probe_cycle is not None and _release_cancelled_probes():
                self._release_probe_slot(probe_cycle)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _transition_to(self, new_state: BreakerState) -> None:
        """Transition to new state (caller must hold lock)."""
        old_state = self._state
        self._state = new_state
        self._state_changes += 1
        self._last_state_change_time = time.monotonic()

        if new_state == BreakerState.OPEN:
            self._opened_at = time.monotonic()
            self._half_open_calls = 0

        if new_state == BreakerState.CLOSED:
            self._consecutive_failures = 0
            self._half_open_calls = 0

        if new_state == BreakerState.HALF_OPEN:
            self._half_open_calls = 0
            self._half_open_cycle += 1

        logger.warning(
            "CircuitBreaker '%s': %s → %s (failures=%d)",
            self.name,
            old_state.value,
            new_state.value,
            self._consecutive_failures,
        )

    def _release_probe_slot(self, probe_cycle: int) -> None:
        """Return a HALF_OPEN probe slot taken in ``probe_cycle`` (RU-094).

        Synchronous on purpose: with no ``await`` between the check and the
        decrement, a second cancellation cannot interrupt it, and no other
        coroutine can interleave on the event loop. The slot is returned only
        while the breaker is still in the HALF_OPEN cycle the probe belongs to;
        a probe from an older cycle never touches a newer cycle's counter.
        """
        if self._state == BreakerState.HALF_OPEN and self._half_open_cycle == probe_cycle and self._half_open_calls > 0:
            self._half_open_calls -= 1

    def _on_neutral(self, probe_cycle: int | None) -> None:
        """Record an exception the classifier ruled not a failure (RU-093).

        Neither a failure nor a success: the failure streak is neither
        incremented nor reset, and a HALF_OPEN probe slot is returned.
        """
        self._total_neutral_errors += 1
        if probe_cycle is not None:
            self._release_probe_slot(probe_cycle)

    async def _on_success(self) -> None:
        async with self._lock:
            self._total_successes += 1
            if self._state == BreakerState.HALF_OPEN:
                self._transition_to(BreakerState.CLOSED)
            elif self._state == BreakerState.CLOSED:
                self._consecutive_failures = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._total_failures += 1
            self._last_failure_time = time.monotonic()
            self._consecutive_failures += 1

            if self._state == BreakerState.HALF_OPEN:
                self._transition_to(BreakerState.OPEN)
            elif self._state == BreakerState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    self._transition_to(BreakerState.OPEN)

    # ------------------------------------------------------------------
    # Manual controls
    # ------------------------------------------------------------------

    async def force_open(self) -> None:
        async with self._lock:
            self._transition_to(BreakerState.OPEN)

    async def force_close(self) -> None:
        async with self._lock:
            self._transition_to(BreakerState.CLOSED)

    async def reset_metrics(self) -> None:
        async with self._lock:
            self._total_calls = 0
            self._total_successes = 0
            self._total_failures = 0
            self._total_rejections = 0
            self._total_neutral_errors = 0
            self._state_changes = 0
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            self._last_state_change_time = 0.0
