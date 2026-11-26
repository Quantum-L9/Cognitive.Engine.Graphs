"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, graph, driver, neo4j, timeout, pool]
owner: engine-team
status: active
--- /L9_META ---

D8 / ND-DRIVER: GraphDriver bounds.

``GraphDriver.connect()`` hard-coded its pool (50 / 3600 s / 60 s) and ignored
the declared ``neo4j_pool_size``, ``neo4j_max_connection_lifetime`` and
``neo4j_connection_acquisition_timeout``; no query carried a transaction
timeout. These tests pin the contract: the pool comes from Settings, every
query / read / write carries ``neo4j_query_timeout_s`` while
``neo4j_query_timeout_enabled`` is on, nothing is bounded while it is off, and
the timeout is validated to (0, 30) so the server gives up before the 30 s
TransportHeader caller budget.

The neo4j package boundary is replaced by a recording fake; GraphDriver itself
is the real class (this is the unit tier; tests/integration/test_query_timeout.py
drives a real server).
"""

from __future__ import annotations

import warnings
from typing import Any
from unittest.mock import patch

import pytest
from neo4j import Query, unit_of_work
from neo4j.exceptions import ClientError, Neo4jError, TransientError
from pydantic import ValidationError

from engine.config.settings import Settings
from engine.graph.circuit_breaker import BreakerState
from engine.graph.driver import GraphDriver
from engine.handlers import ExecutionError

pytestmark = pytest.mark.unit


def _settings(**overrides: Any) -> Settings:
    # l9_env pinned to dev so a local .env cannot select prod; no credential is needed.
    return Settings(l9_env="dev", **overrides)


class _Result:
    async def data(self) -> list[Any]:
        return [{"ok": 1}]

    async def consume(self) -> Any:
        class _Counters:
            nodes_created = nodes_deleted = relationships_created = 0
            relationships_deleted = properties_set = labels_added = 0

        class _Summary:
            counters = _Counters()

        return _Summary()


class _Tx:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def run(self, cypher: Any, parameters: Any = None) -> _Result:
        self._recorder.tx_runs.append(cypher)
        if self._recorder.fail_with is not None:
            raise self._recorder.fail_with
        return _Result()


class _Session:
    def __init__(self, recorder: _Recorder, database: str) -> None:
        self._recorder = recorder
        self._database = database

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run(self, statement: Any, parameters: Any = None) -> _Result:
        self._recorder.runs.append((statement, self._database))
        if self._recorder.fail_with is not None and self._database != "system":
            raise self._recorder.fail_with
        return _Result()

    async def _managed(self, kind: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        self._recorder.tx_functions.append((kind, getattr(func, "timeout", None), getattr(func, "metadata", None)))
        return await func(_Tx(self._recorder), *args, **kwargs)

    async def execute_read(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._managed("read", func, *args, **kwargs)

    async def execute_write(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._managed("write", func, *args, **kwargs)


class _Recorder:
    def __init__(self) -> None:
        self.driver_kwargs: dict[str, Any] = {}
        self.runs: list[tuple[Any, str]] = []
        self.tx_runs: list[Any] = []
        self.tx_functions: list[tuple[str, float | None, Any]] = []
        self.fail_with: Exception | None = None


class _FakeAsyncDriver:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def verify_connectivity(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def session(self, database: str = "neo4j", **_: Any) -> _Session:
        return _Session(self._recorder, database)


async def _connected(recorder: _Recorder) -> GraphDriver:
    def _factory(uri: str, **kwargs: Any) -> _FakeAsyncDriver:
        recorder.driver_kwargs = dict(kwargs)
        return _FakeAsyncDriver(recorder)

    driver = GraphDriver(uri="bolt://fake:7687")
    with patch("engine.graph.driver.AsyncGraphDatabase.driver", side_effect=_factory):
        await driver.connect()
    return driver


async def _exercise(driver: GraphDriver) -> None:
    await driver.execute_query("RETURN 1 AS ok", database="neo4j")
    await driver.execute_read("RETURN 1 AS ok", database="neo4j")
    await driver.execute_write(cypher="CREATE (n:T) RETURN 1 AS ok", database="neo4j")

    async def _caller_tx(tx: Any) -> str:
        await tx.run("RETURN 2")
        return "done"

    assert await driver.execute_write(_caller_tx, database="neo4j") == "done"


# ── Settings contract ───────────────────────────────────────────────────────


def test_query_timeout_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The code defaults, not whatever a developer exported.
    monkeypatch.delenv("NEO4J_QUERY_TIMEOUT_S", raising=False)
    monkeypatch.delenv("NEO4J_QUERY_TIMEOUT_ENABLED", raising=False)
    s = _settings()
    assert s.neo4j_query_timeout_enabled is True
    assert s.neo4j_query_timeout_s == 25.0
    assert 0 < s.neo4j_query_timeout_s < 30


@pytest.mark.parametrize("bad", [0, -1, 30, 30.0, 45, float("inf"), float("nan")])
def test_query_timeout_outside_open_interval_is_refused(bad: float) -> None:
    with pytest.raises(ValidationError, match="neo4j_query_timeout_s"):
        _settings(neo4j_query_timeout_s=bad)


@pytest.mark.parametrize("good", [0.001, 1, 25, 29.999])
def test_query_timeout_inside_open_interval_is_accepted(good: float) -> None:
    assert _settings(neo4j_query_timeout_s=good).neo4j_query_timeout_s == good


# ── Pool from Settings ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_driver_pool_is_built_from_settings() -> None:
    recorder = _Recorder()
    overrides = _settings(
        neo4j_pool_size=7,
        neo4j_max_connection_lifetime=123,
        neo4j_connection_acquisition_timeout=9,
    )
    with patch("engine.config.settings.settings", overrides):
        driver = await _connected(recorder)
    assert recorder.driver_kwargs["max_connection_pool_size"] == 7
    assert recorder.driver_kwargs["max_connection_lifetime"] == 123
    assert recorder.driver_kwargs["connection_acquisition_timeout"] == 9
    assert recorder.driver_kwargs["auth"] == (driver.username, driver.password)


# ── Transaction timeout on every query ──────────────────────────────────────


@pytest.mark.asyncio
async def test_every_query_carries_configured_timeout() -> None:
    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings(neo4j_query_timeout_s=4.5)):
        driver = await _connected(recorder)
        await _exercise(driver)

    # execute_query: session.run receives a timed Query, not bare text.
    assert len(recorder.runs) == 1
    statement, database = recorder.runs[0]
    assert database == "neo4j"
    assert isinstance(statement, Query)
    assert statement.text == "RETURN 1 AS ok"
    assert statement.timeout == 4.5

    # execute_read, execute_write(cypher=), execute_write(callable): every managed
    # transaction function carries the timeout.
    assert [kind for kind, _, _ in recorder.tx_functions] == ["read", "write", "write"]
    assert [timeout for _, timeout, _ in recorder.tx_functions] == [4.5, 4.5, 4.5]


@pytest.mark.asyncio
async def test_flag_off_applies_no_timeout() -> None:
    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings(neo4j_query_timeout_enabled=False)):
        driver = await _connected(recorder)
        await _exercise(driver)

    statement, _ = recorder.runs[0]
    assert statement == "RETURN 1 AS ok", "flag off: the bare statement, exactly as before"
    assert [timeout for _, timeout, _ in recorder.tx_functions] == [None, None, None]


@pytest.mark.asyncio
async def test_caller_configured_unit_of_work_keeps_its_own_timeout_and_metadata() -> None:
    recorder = _Recorder()

    @unit_of_work(metadata={"app": "caller"}, timeout=2.0)
    async def _configured(tx: Any) -> str:
        await tx.run("RETURN 3")
        return "kept"

    @unit_of_work(metadata={"app": "meta-only"})
    async def _meta_only(tx: Any) -> str:
        return "meta"

    with patch("engine.config.settings.settings", _settings(neo4j_query_timeout_s=10.0)):
        driver = await _connected(recorder)
        assert await driver.execute_write(_configured, database="neo4j") == "kept"
        assert await driver.execute_write(_meta_only, database="neo4j") == "meta"

    assert recorder.tx_functions == [("write", 2.0, {"app": "caller"}), ("write", 10.0, {"app": "meta-only"})]
    # The caller's function object is not mutated by the bound applied around it.
    assert _meta_only.timeout is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_system_database_administrative_command_is_not_bounded() -> None:
    """Provisioning's CREATE DATABASE ... WAIT runs on `system` and carries its own WAIT bound."""
    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings(auto_create_domain_database=True)):
        driver = await _connected(recorder)
        assert await driver.ensure_database("plasticos") is True

    assert recorder.runs == [("CREATE DATABASE `plasticos` IF NOT EXISTS WAIT", "system")]


# ── A terminated statement surfaces as the engine's typed ExecutionError ───


def _server_error(code: str) -> Neo4jError:
    """The error the neo4j driver raises for a server failure with ``code``.

    ``Neo4jError.hydrate`` is how the driver itself builds these from a server
    FAILURE message (it picks the subclass from the code); it warns that it is
    internal, which is expected here.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return Neo4jError.hydrate(code=code, message="The transaction has been terminated.")


CLIENT_CONFIGURED_TIMEOUT = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
SERVER_CONFIGURED_TIMEOUT = "Neo.ClientError.Transaction.TransactionTimedOut"
LEGACY_TRANSIENT_TIMEOUT = "Neo.TransientError.Transaction.TransactionTimedOut"


async def _calls(driver: GraphDriver) -> list[Any]:
    async def _caller_tx(tx: Any) -> None:
        await tx.run("RETURN 2")

    return [
        lambda: driver.execute_query("RETURN 1", database="plasticos"),
        lambda: driver.execute_read("RETURN 1", database="plasticos"),
        lambda: driver.execute_write(cypher="RETURN 1", database="plasticos"),
        lambda: driver.execute_write(_caller_tx, database="plasticos"),
    ]


@pytest.mark.parametrize("code", [CLIENT_CONFIGURED_TIMEOUT, SERVER_CONFIGURED_TIMEOUT, LEGACY_TRANSIENT_TIMEOUT])
@pytest.mark.asyncio
async def test_transaction_timeout_raises_execution_error_with_driver_cause(code: str) -> None:
    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings(neo4j_query_timeout_s=3.0)):
        driver = await _connected(recorder)
        for call in await _calls(driver):
            recorder.fail_with = _server_error(code)
            with pytest.raises(ExecutionError) as raised:
                await call()
            error = raised.value
            assert isinstance(error.__cause__, Neo4jError)
            assert error.__cause__ is recorder.fail_with
            assert error.__cause__.code == code
            assert error.action == "graph_query"
            assert error.tenant == "plasticos"
            assert error.detail == code
            assert "neo4j_query_timeout_s=3" in str(error)


@pytest.mark.asyncio
async def test_neo4j_5_timeout_codes_are_client_errors_and_never_open_the_breaker() -> None:
    """Neo4j 5 reports both timeout codes as ClientError, which the shared breaker does not count."""
    assert isinstance(_server_error(CLIENT_CONFIGURED_TIMEOUT), ClientError)
    assert isinstance(_server_error(SERVER_CONFIGURED_TIMEOUT), ClientError)
    assert isinstance(_server_error(LEGACY_TRANSIENT_TIMEOUT), TransientError)

    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings()):
        driver = await _connected(recorder)
        for _ in range(driver.circuit_breaker.failure_threshold + 2):
            recorder.fail_with = _server_error(CLIENT_CONFIGURED_TIMEOUT)
            with pytest.raises(ExecutionError):
                await driver.execute_query("RETURN 1", database="plasticos")
        assert driver.circuit_breaker.state == BreakerState.CLOSED

        recorder.fail_with = None
        assert await driver.execute_query("RETURN 1", database="plasticos") == [{"ok": 1}]


@pytest.mark.asyncio
async def test_flag_off_server_timeout_is_still_typed_and_names_the_server_bound() -> None:
    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings(neo4j_query_timeout_enabled=False)):
        driver = await _connected(recorder)
        recorder.fail_with = _server_error(SERVER_CONFIGURED_TIMEOUT)
        with pytest.raises(ExecutionError, match="db.transaction.timeout") as raised:
            await driver.execute_query("RETURN 1", database="plasticos")
    assert raised.value.__cause__ is recorder.fail_with


@pytest.mark.parametrize(
    "code",
    ["Neo.ClientError.Statement.SyntaxError", "Neo.TransientError.Transaction.DeadlockDetected"],
)
@pytest.mark.asyncio
async def test_non_timeout_server_errors_pass_through_unchanged(code: str) -> None:
    recorder = _Recorder()
    with patch("engine.config.settings.settings", _settings()):
        driver = await _connected(recorder)
        recorder.fail_with = _server_error(code)
        with pytest.raises(Neo4jError) as raised:
            await driver.execute_query("RETURN 1", database="plasticos")
    assert raised.value is recorder.fail_with
    assert not isinstance(raised.value, ExecutionError)
