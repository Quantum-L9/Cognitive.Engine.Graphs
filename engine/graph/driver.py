"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [graph, driver, neo4j]
owner: engine-team
status: active
--- /L9_META ---

Neo4j driver wrapper.
Manages connection pooling and multi-database routing.
"""

import asyncio
import functools
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, Query, unit_of_work
from neo4j import exceptions as neo4j_exceptions

from engine.graph.circuit_breaker import BreakerState, CircuitBreaker
from engine.utils.security import sanitize_database_name

logger = logging.getLogger(__name__)

# Databases the DBMS always provides; never candidates for provisioning.
_BUILTIN_DATABASES = frozenset({"neo4j", "system"})

# Substrings Neo4j uses when the target database is absent. Matched case
# insensitively against the driver's message.
_DATABASE_ABSENT_MARKERS = (
    "database does not exist",
    "databasenotfound",
    "unable to get a routing table for database",
)


class DatabaseNotProvisionedError(RuntimeError):
    """A query named a database the DBMS does not have.

    CEG-008: `match` and `sync` route to a database named after the domain id,
    and nothing created it. Neo4j does not create databases implicitly, so on a
    fresh instance every sync and match failed with the driver's own message
    until an operator happened to run CREATE DATABASE by hand. This names the
    missing database and the command that provides it.
    """


def _is_breaker_failure(exc: BaseException) -> bool:
    """RU-093 / CEG-COR-034: does this exception count toward opening the breaker?

    The Neo4j breaker is process-wide, so one tenant's client errors
    (CypherSyntaxError, ConstraintError, DatabaseNotFound, a null MERGE key)
    must not fail every other tenant fast. ``neo4j.exceptions.ClientError`` and
    its subclasses are per-request faults: not failures — except the credential
    classes ``AuthError`` (incl. ``TokenExpired``) and ``Forbidden``, which are
    process-wide and count (owner decision 2026-10-29). Infrastructure and
    transient faults (ServiceUnavailable, SessionExpired, TransientError,
    DatabaseError, OSError, anything else) still count. With
    ``neo4j_circuit_ignore_client_errors`` off every exception counts.
    """
    from engine.config.settings import settings

    if not settings.neo4j_circuit_ignore_client_errors:
        return True
    if isinstance(exc, _CREDENTIAL_CLIENT_ERRORS):
        return True
    return not isinstance(exc, neo4j_exceptions.ClientError)


# ClientError subclasses that are not per-request faults (TokenExpired is an AuthError).
_CREDENTIAL_CLIENT_ERRORS = (neo4j_exceptions.AuthError, neo4j_exceptions.Forbidden)


def _provision_inside_breaker() -> bool:
    """RU-095 / CEG-COR-170 flag: admit through the breaker before provisioning.

    On: an OPEN breaker rejects in O(1) before any CREATE DATABASE is issued.
    Off: the legacy provision-then-admit ordering.
    """
    from engine.config.settings import settings

    return settings.neo4j_provision_inside_breaker


def _query_timeout() -> float | None:
    """D8: the transaction timeout every engine query carries, or None when the flag is off.

    ``neo4j_query_timeout_s`` is validated to (0, 30) by ``Settings``, so the
    server abandons a statement before the 30 s TransportHeader caller budget.
    """
    from engine.config.settings import settings

    if not settings.neo4j_query_timeout_enabled:
        return None
    return float(settings.neo4j_query_timeout_s)


def _bounded_statement(cypher: str, timeout: float | None) -> str | Query:
    """The statement ``session.run`` receives: a timed ``Query``, or the bare text when unbounded."""
    if timeout is None:
        return cypher
    return Query(cypher, timeout=timeout)


def _bounded_transaction(func: Any, timeout: float | None) -> Any:
    """A transaction function carrying ``timeout``, unless it is unbounded or already configured.

    A function the caller decorated with ``neo4j.unit_of_work(timeout=...)``
    keeps its own timeout; its metadata is carried over either way.
    ``unit_of_work`` wraps rather than mutates, so the caller's function is untouched.
    """
    if timeout is None or getattr(func, "timeout", None) is not None:
        return func
    return unit_of_work(metadata=getattr(func, "metadata", None), timeout=timeout)(func)


def _looks_like_absent_database(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _DATABASE_ABSENT_MARKERS)


class ExpectedWriteOutcome(Exception):  # noqa: N818 - an outcome, not an error of the store
    """A write the server answered and the caller refuses: not an outage.

    Raised from inside a write transaction function to roll it back (for example
    a post-write check that finds the stored state differs from what was sent).
    Like a ``ConstraintError``, it never counts against the shared circuit breaker.
    """


def _is_expected_write_outcome(exc: Exception) -> bool:
    from neo4j.exceptions import ConstraintError

    return isinstance(exc, ConstraintError | ExpectedWriteOutcome)


class _BreakerNeutral(BaseException):
    """An outcome that is neither a success nor a failure of the server.

    ``CircuitBreaker.call`` records an ``Exception`` as a failure and a return as
    a success. An absent tenant database, a uniqueness-constraint violation or a
    rolled-back post-write check is an expected answer, not an outage: counting
    it would let one tenant's configuration or one lost identity race trip the
    breaker shared by every tenant, and a sentinel return would reset a genuine
    run of consecutive failures. A ``BaseException`` passes through the breaker
    without either outcome (as ``asyncio.CancelledError`` does) and is unwrapped
    before it leaves the driver.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _AnsweredOnProbe:
    """Returned instead of raised when a HALF_OPEN probe gets an expected answer.

    The server answered, so the probe genuinely succeeded; a neutral exit would
    leave the probe slot it consumed unreleased. Unwrapped and re-raised by the
    caller.
    """

    def __init__(self, cause: Exception) -> None:
        self.cause = cause


def _unwrap_breaker_outcomes(
    method: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Re-raise an expected answer that crossed the breaker as ``_BreakerNeutral``
    or ``_AnsweredOnProbe`` (see ``GraphDriver._classified``)."""

    @functools.wraps(method)
    async def unwrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await method(*args, **kwargs)
        except _BreakerNeutral as neutral:
            raise neutral.cause from None
        if isinstance(result, _AnsweredOnProbe):
            raise result.cause
        return result

    return unwrapped


class GraphDriver:
    """Neo4j async driver manager."""

    def __init__(
        self,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        """
        Initialize driver.

        Args:
            uri: Neo4j URI (defaults to NEO4J_URI env var)
            username: Neo4j username (defaults to NEO4J_USERNAME env var)
            password: Neo4j password (defaults to NEO4J_PASSWORD env var)
        """
        self.uri: str = uri or os.getenv("NEO4J_URI") or "bolt://localhost:7687"
        self.username: str = username or os.getenv("NEO4J_USERNAME") or "neo4j"
        self.password: str = password or os.getenv("NEO4J_PASSWORD") or "password"

        self._driver: AsyncDriver | None = None
        self._lock = asyncio.Lock()
        # Databases this process has already provisioned or confirmed, so the
        # CREATE is attempted at most once per database per process ...
        self._ensured_databases: set[str] = set()
        # ... and the CREATE currently in flight per database, kept apart from
        # the ensured set so a concurrent first use awaits that CREATE instead
        # of either issuing its own or racing past it into the domain query.
        self._provisioning: dict[str, asyncio.Task[bool]] = {}

        # W4-02: Circuit breaker — configured via settings, defaults provided
        from engine.config.settings import settings

        self._circuit_breaker = CircuitBreaker(
            name="neo4j",
            failure_threshold=settings.neo4j_circuit_threshold,
            recovery_timeout=settings.neo4j_circuit_cooldown,
            half_open_max_calls=settings.neo4j_circuit_half_open_max,
            is_failure=_is_breaker_failure,
        )

    async def connect(self) -> None:
        """Establish driver connection."""
        async with self._lock:
            if self._driver:
                logger.warning("Driver already connected")
                return

            # D8: the pool comes from the declared settings, not literals.
            from engine.config.settings import settings

            logger.info(f"Connecting to Neo4j at {self.uri}")
            self._driver = AsyncGraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                max_connection_lifetime=settings.neo4j_max_connection_lifetime,
                max_connection_pool_size=settings.neo4j_pool_size,
                connection_acquisition_timeout=settings.neo4j_connection_acquisition_timeout,
            )

            # Verify connectivity
            await self._driver.verify_connectivity()
            logger.info("Neo4j driver connected successfully")

    async def close(self) -> None:
        """Close driver connection."""
        async with self._lock:
            if self._driver:
                await self._driver.close()
                self._driver = None
                logger.info("Neo4j driver closed")

    def get_driver(self) -> AsyncDriver:
        """Get driver instance."""
        if not self._driver:
            raise RuntimeError("Driver not connected. Call connect() first.")
        return self._driver

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Expose circuit breaker for health probes and metrics."""
        return self._circuit_breaker

    async def _raw_execute_query(
        self,
        cypher: str,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
    ) -> list[Any]:
        """Execute Cypher query without circuit breaker (internal).

        D8: the statement carries the configured transaction timeout, except on
        the ``system`` database, where only administrative commands run (the
        provisioning ``CREATE DATABASE ... WAIT`` waits on the DBMS, not on a
        domain statement, and has its own WAIT bound).
        """
        driver = self.get_driver()
        statement = _bounded_statement(cypher, None if database == "system" else _query_timeout())

        async with driver.session(database=database) as session:
            result = await session.run(statement, parameters or {})
            records: list[Any] = await result.data()
            return records

    async def execute_query(
        self,
        cypher: str,
        parameters: dict[str, Any] | None = None,
        database: str | None = None,
    ) -> list[Any]:
        """Execute Cypher query with W4-02 circuit breaker protection.

        Raises CircuitOpenError (maps to 503) if breaker is OPEN.
        """
        database = self._resolve_database(database, "execute_query")
        if _provision_inside_breaker():
            self._precheck_provisionable(database)
            query = self._provisioned_raw_execute_query
        else:
            await self._provision_on_first_use(database)
            query = self._raw_execute_query
        try:
            return await self._circuit_breaker.call(query, cypher, parameters, database)
        except Exception as exc:
            raise self._translate_absent_database(exc, database) from exc

    async def _raw_execute_read(
        self,
        cypher: str,
        parameters: dict[str, Any] | None,
        database: str,
    ) -> list[Any]:
        """Run ``cypher`` in a managed READ transaction (internal, no circuit breaker)."""
        driver = self.get_driver()

        async def _read(tx: Any) -> list[Any]:
            result = await tx.run(cypher, parameters or {})
            records: list[Any] = await result.data()
            return records

        from neo4j import READ_ACCESS

        async with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
            records: list[Any] = await session.execute_read(_bounded_transaction(_read, _query_timeout()))
            return records

    async def _classified(
        self,
        is_expected: Callable[[Exception], bool],
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run ``func`` so that an expected answer reaches the breaker as neither outcome."""
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            if not is_expected(exc):
                raise
            if self._circuit_breaker.state == BreakerState.HALF_OPEN:
                return _AnsweredOnProbe(exc)
            raise _BreakerNeutral(exc) from exc

    async def _call_breaker_neutral(
        self,
        is_expected: Callable[[Exception], bool],
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """``CircuitBreaker.call`` with expected answers re-raised to the caller uncounted."""
        try:
            result = await self._circuit_breaker.call(self._classified, is_expected, func, *args, **kwargs)
        except _BreakerNeutral as neutral:
            raise neutral.cause from None
        if isinstance(result, _AnsweredOnProbe):
            raise result.cause
        return result

    async def execute_read(
        self,
        cypher: str,
        parameters: dict[str, Any] | None = None,
        database: str | None = None,
    ) -> list[Any]:
        """Read-only query that never provisions a database (QM.POC0.CEG.GRAPH_EVIDENCE.002A).

        Unlike ``execute_query`` this skips ``_provision_on_first_use`` even
        under ``auto_create_domain_database``, so a read can never CREATE a
        tenant database, and runs in a managed READ transaction, so the server
        refuses any write the statement attempts. An absent database surfaces
        as ``DatabaseNotProvisionedError``. Same explicit-database rule and
        circuit breaker as ``execute_query``, except that an absent database
        is breaker-neutral: it neither counts as a failure nor resets a run of
        genuine failures (see ``_BreakerNeutral``).
        """
        database = self._resolve_database(database, "execute_read")

        def absent(exc: Exception) -> bool:
            return _looks_like_absent_database(exc) and database not in _BUILTIN_DATABASES

        try:
            records: list[Any] = await self._call_breaker_neutral(
                absent, self._raw_execute_read, cypher, parameters, database
            )
        except Exception as exc:
            raise self._translate_absent_database(exc, database) from exc
        return records

    @staticmethod
    def _resolve_database(database: str | None, method: str) -> str:
        """W7-01 / DB-STRICT-01: an omitted ``database=`` is an error by default.

        Every tenant's data lives in a database named after its domain id, so a
        call that names no database can only mean the caller forgot — and the
        old silent fallback put that caller's reads and writes on the shared
        ``neo4j`` database. ``strict_tenant_database`` ships True since the
        2026-10-22 caller audit; ``STRICT_TENANT_DATABASE=false`` restores the
        fallback for a deployment that still has an unaudited caller.
        """
        if database is not None:
            return database
        from engine.config.settings import settings

        if settings.strict_tenant_database:
            msg = (
                f"GraphDriver.{method} requires an explicit database= (W7-01): tenant data lives in a "
                "database named after the domain id and there is no implicit 'neo4j' fallback. "
                "Pass database=<domain id>, or set STRICT_TENANT_DATABASE=false to restore the fallback."
            )
            raise ValueError(msg)
        return "neo4j"

    async def _provision_on_first_use(self, database: str) -> None:
        """CEG-008: a tenant domain database has to exist before it can be used.

        Under ``auto_create_domain_database`` we provision it on first use —
        for reads and writes alike, since a fresh deployment's first operation
        is as likely to be a sync write as a match query.
        """
        from engine.config.settings import settings as _db_settings

        if _db_settings.auto_create_domain_database:
            await self.ensure_database(database)

    def _precheck_provisionable(self, database: str) -> None:
        """RU-095: refuse an unprovisionable name before breaker admission.

        Pure validation, no I/O. It keeps a malformed database name a caller
        error (``ValueError``, raised exactly as before) rather than a failure
        counted by the breaker now that provisioning runs inside admission.
        """
        from engine.config.settings import settings as _db_settings

        if (
            _db_settings.auto_create_domain_database
            and database not in _BUILTIN_DATABASES
            and database not in self._ensured_databases
        ):
            sanitize_database_name(database)

    async def _provisioned_raw_execute_query(
        self,
        cypher: str,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
    ) -> list[Any]:
        """RU-095: first-use provisioning, then the query — both inside breaker admission."""
        await self._provision_on_first_use(database)
        return await self._raw_execute_query(cypher, parameters, database)

    async def _provisioned_raw_execute_write(
        self,
        transaction_function: Any = None,
        *args: Any,
        cypher: str | None = None,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
        **kwargs: Any,
    ) -> dict[str, Any] | Any:
        """RU-095: first-use provisioning, then the write — both inside breaker admission."""
        await self._provision_on_first_use(database)
        return await self._raw_execute_write(
            transaction_function,
            *args,
            cypher=cypher,
            parameters=parameters,
            database=database,
            **kwargs,
        )

    @staticmethod
    def _translate_absent_database(exc: Exception, database: str) -> Exception:
        """Name the missing database and the command that provides it, else pass the error through."""
        if _looks_like_absent_database(exc) and database not in _BUILTIN_DATABASES:
            msg = (
                f"Neo4j database {database!r} does not exist. Domain queries route to a "
                f"database named after the domain id, and Neo4j does not create one "
                f"implicitly. Run this against the system database (Enterprise "
                f"Edition):  CREATE DATABASE `{database}` IF NOT EXISTS WAIT  "
                f"-- or set AUTO_CREATE_DOMAIN_DATABASE=true to have the engine "
                f"create it on first use."
            )
            return DatabaseNotProvisionedError(msg)
        return exc

    async def ensure_database(self, name: str) -> bool:
        """Create the domain database if it is absent. Idempotent per process.

        Returns True when the database is known to exist afterwards, False when
        provisioning was not possible (Community Edition, or insufficient
        privileges) — in which case the query that follows fails with the
        message above rather than here, so a read-only deployment that has the
        database already is not blocked by a CREATE it is not allowed to run.

        Concurrency: the first caller for a name starts the CREATE; every caller
        that arrives while it is in flight awaits that same CREATE and receives
        its result, so no domain query proceeds before provisioning completes
        and the administrative command is issued once. A failed CREATE leaves
        nothing ensured, so a later call retries it.
        """
        if name in _BUILTIN_DATABASES or name in self._ensured_databases:
            return True
        sanitize_database_name(name)

        task = self._provisioning.get(name)
        if task is None or task.done():
            # A finished task still in the map lost its race with the done
            # callback below; its outcome is already reflected in
            # `_ensured_databases` (success) or not (failure → retry now).
            task = asyncio.get_running_loop().create_task(self._provision_database(name))
            self._provisioning[name] = task
            task.add_done_callback(functools.partial(self._forget_provisioning, name))
        # shield: cancelling one waiting request must not cancel the CREATE the
        # other waiters — and the ensured set — depend on.
        return await asyncio.shield(task)

    def _forget_provisioning(self, name: str, done: asyncio.Task[bool]) -> None:
        if self._provisioning.get(name) is done:
            del self._provisioning[name]

    async def _provision_database(self, name: str) -> bool:
        # Administrative commands must run against `system`, and the name is
        # back-quoted because a dash is legal in a database name but not in a
        # bare identifier. sanitize_database_name is what makes that quoting
        # safe; it is re-applied here so the statement is safe by construction.
        cypher = f"CREATE DATABASE `{sanitize_database_name(name)}` IF NOT EXISTS WAIT"
        try:
            await self._raw_execute_query(cypher, None, "system")
        except Exception as exc:
            logger.warning(
                "Could not provision Neo4j database %r (%s: %s). "
                "CREATE DATABASE is Enterprise Edition only and requires admin privileges.",
                name,
                type(exc).__name__,
                exc,
            )
            return False

        self._ensured_databases.add(name)
        logger.info("Ensured Neo4j database %r exists", name)
        return True

    async def _raw_execute_write(
        self,
        transaction_function: Any = None,
        *args: Any,
        cypher: str | None = None,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
        **kwargs: Any,
    ) -> dict[str, Any] | Any:
        """The write the breaker runs: ``_write_once`` with expected answers classified.

        A ``ConstraintError`` or ``ExpectedWriteOutcome`` leaves as
        ``_BreakerNeutral`` (or ``_AnsweredOnProbe`` on a HALF_OPEN probe), so the
        breaker records neither outcome; ``execute_write`` unwraps it.
        """
        return await self._classified(
            _is_expected_write_outcome,
            self._write_once,
            transaction_function,
            *args,
            cypher=cypher,
            parameters=parameters,
            database=database,
            **kwargs,
        )

    async def _write_once(
        self,
        transaction_function: Any = None,
        *args: Any,
        cypher: str | None = None,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
        **kwargs: Any,
    ) -> dict[str, Any] | Any:
        """Execute a write without circuit breaker (internal).

        Supports two calling conventions:
        - ``cypher=...`` keyword form: runs the statement in a managed write
          transaction and returns a summary dict with Neo4j counters
          (``nodes_created``, ``relationships_created``, ...) plus
          ``records`` and ``status``.
        - callable form: delegates to the session's managed
          ``execute_write(transaction_function, *args, **kwargs)`` and
          returns whatever the transaction function returns.
        """
        driver = self.get_driver()

        if transaction_function is not None and cypher is not None:
            raise ValueError("Pass either a transaction function or cypher=, not both")
        if transaction_function is None and cypher is None:
            raise ValueError("execute_write requires a transaction function or cypher=")

        timeout = _query_timeout()
        async with driver.session(database=database) as session:
            if transaction_function is not None:
                return await session.execute_write(_bounded_transaction(transaction_function, timeout), *args, **kwargs)

            async def _run_statement(tx: Any) -> dict[str, Any]:
                result = await tx.run(cypher, parameters or {})
                records: list[Any] = await result.data()
                summary = await result.consume()
                counters = summary.counters
                return {
                    "status": "ok",
                    "records": records,
                    "nodes_created": counters.nodes_created,
                    "nodes_deleted": counters.nodes_deleted,
                    "relationships_created": counters.relationships_created,
                    "relationships_deleted": counters.relationships_deleted,
                    "properties_set": counters.properties_set,
                    "labels_added": counters.labels_added,
                }

            return await session.execute_write(_bounded_transaction(_run_statement, timeout))

    @_unwrap_breaker_outcomes
    async def execute_write(
        self,
        transaction_function: Any = None,
        *args: Any,
        cypher: str | None = None,
        parameters: dict[str, Any] | None = None,
        database: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | Any:
        """Execute a write with W4-02 circuit breaker protection.

        Mirrors :meth:`execute_query` semantics for writes. Accepts either a
        managed transaction function (neo4j-driver style) or a
        ``cypher=``/``parameters=`` keyword form returning a counters summary
        dict. Raises CircuitOpenError (maps to 503) if breaker is OPEN.

        A ``ConstraintError`` or ``ExpectedWriteOutcome`` is the server's answer,
        not an outage: it reaches the caller unchanged and never counts against
        the shared breaker (see ``_BreakerNeutral``).
        """
        database = self._resolve_database(database, "execute_write")
        if _provision_inside_breaker():
            self._precheck_provisionable(database)
            write = self._provisioned_raw_execute_write
        else:
            await self._provision_on_first_use(database)
            write = self._raw_execute_write
        try:
            return await self._circuit_breaker.call(
                write,
                transaction_function,
                *args,
                cypher=cypher,
                parameters=parameters,
                database=database,
                **kwargs,
            )
        except Exception as exc:
            raise self._translate_absent_database(exc, database) from exc
