"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, graph, driver, neo4j, timeout]
owner: engine-team
status: active
--- /L9_META ---

D8 / ND-DRIVER against a real Neo4j: a statement that runs past
``neo4j_query_timeout_s`` is terminated by the server, leaves ``GraphDriver`` as
the engine's typed ``ExecutionError`` whose ``__cause__`` is the neo4j driver's
transaction-timeout error (``Neo.ClientError.Transaction.TransactionTimedOut*``),
and gives its pooled connection back.

The driver under test is a fresh ``GraphDriver`` with a one-connection pool, so
a connection the timed-out statement kept would make the follow-up query wait
out ``neo4j_connection_acquisition_timeout`` and fail. The slow statement is a
real busy loop (``UNWIND range(...)`` with a filter), not a mock or a sleep
procedure, and needs no APOC.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from neo4j.exceptions import ClientError

from engine.config.settings import Settings
from engine.graph.circuit_breaker import BreakerState
from engine.graph.driver import GraphDriver
from engine.handlers import ExecutionError

pytestmark = pytest.mark.integration

TIMEOUT_S = 1.0
# Far more work than one second of execution on any test server.
SLOW_CYPHER = "UNWIND range(1, 2000000000) AS i WITH i WHERE i % 7 = 3 RETURN count(i) AS c"


def _bounded_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "neo4j_query_timeout_enabled": True,
        "neo4j_query_timeout_s": TIMEOUT_S,
        "neo4j_pool_size": 1,
        "neo4j_connection_acquisition_timeout": 5,
    }
    values.update(overrides)
    # l9_env pinned to dev so a local .env cannot select prod; no credential is needed.
    return Settings(l9_env="dev", **values)


@pytest_asyncio.fixture
async def bounded_driver(neo4j_container: dict[str, str]) -> AsyncGenerator[GraphDriver, None]:
    with patch("engine.config.settings.settings", _bounded_settings()):
        driver = GraphDriver(
            uri=neo4j_container["uri"],
            username=neo4j_container["user"],
            password=neo4j_container["password"],
        )
        await driver.connect()
        try:
            yield driver
        finally:
            await driver.close()


async def _assert_times_out(call: Any) -> float:
    started = time.monotonic()
    with pytest.raises(ExecutionError) as raised:
        await call()
    elapsed = time.monotonic() - started
    cause = raised.value.__cause__
    # Neo4j 5 reports a client-configured timeout as a ClientError (Neo4j status codes).
    assert isinstance(cause, ClientError), f"expected the driver's timeout error as __cause__, got {cause!r}"
    assert cause.code.startswith("Neo.ClientError.Transaction.TransactionTimedOut"), cause.code
    assert raised.value.action == "graph_query"
    assert raised.value.tenant == "neo4j"
    # Terminated near the bound, far below the busy loop's natural runtime and
    # below the 30 s caller budget the setting is validated against.
    assert elapsed < TIMEOUT_S + 10, f"statement ran {elapsed:.1f}s against a {TIMEOUT_S}s bound"
    return elapsed


@pytest.mark.asyncio
async def test_execute_query_past_timeout_raises_and_releases_connection(bounded_driver: GraphDriver) -> None:
    with patch("engine.config.settings.settings", _bounded_settings()):
        await _assert_times_out(lambda: bounded_driver.execute_query(SLOW_CYPHER, database="neo4j"))

        # One-connection pool: this succeeds only if the timed-out statement released it.
        started = time.monotonic()
        rows = await bounded_driver.execute_query("RETURN 1 AS ok", database="neo4j")
        assert rows == [{"ok": 1}]
        assert time.monotonic() - started < 5, "follow-up waited on a connection the timeout kept"


@pytest.mark.asyncio
async def test_execute_read_and_write_past_timeout_raise_and_release(bounded_driver: GraphDriver) -> None:
    with patch("engine.config.settings.settings", _bounded_settings()):
        await _assert_times_out(lambda: bounded_driver.execute_read(SLOW_CYPHER, database="neo4j"))
        await _assert_times_out(lambda: bounded_driver.execute_write(cypher=SLOW_CYPHER, database="neo4j"))

        async def _slow_tx(tx: Any) -> int:
            result = await tx.run(SLOW_CYPHER)
            record = await result.single()
            return int(record["c"])

        await _assert_times_out(lambda: bounded_driver.execute_write(_slow_tx, database="neo4j"))

        rows = await bounded_driver.execute_read("RETURN 2 AS ok", database="neo4j")
        assert rows == [{"ok": 2}]


@pytest.mark.asyncio
async def test_repeated_timeouts_never_exhaust_the_pool_or_open_the_breaker(bounded_driver: GraphDriver) -> None:
    """More timeouts than the breaker threshold on a one-connection pool: each is a per-request answer."""
    with patch("engine.config.settings.settings", _bounded_settings()):
        threshold = bounded_driver.circuit_breaker.failure_threshold
        for _ in range(threshold + 1):
            await _assert_times_out(lambda: bounded_driver.execute_query(SLOW_CYPHER, database="neo4j"))

        assert bounded_driver.circuit_breaker.state == BreakerState.CLOSED
        rows = await bounded_driver.execute_query("RETURN 3 AS ok", database="neo4j")
        assert rows == [{"ok": 3}]
