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
import logging
import os
import re
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from engine.graph.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# Databases the DBMS always provides; never candidates for provisioning.
_BUILTIN_DATABASES = frozenset({"neo4j", "system"})

# Neo4j database naming rules: begins with an ASCII letter, then letters,
# digits, dots, dashes or underscores, 3-63 characters. CREATE DATABASE cannot
# take the name as a query parameter (it is an administrative command, not a
# read/write query), so the name is quoted into the statement — and therefore
# must be validated first. Domain ids legitimately contain dashes
# ("healthcare-referral"), which is why engine.utils.security.sanitize_label
# does not apply here: its label grammar forbids them.
_DATABASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,62}$")

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


def _looks_like_absent_database(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _DATABASE_ABSENT_MARKERS)


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
        # CREATE is attempted at most once per database per process.
        self._ensured_databases: set[str] = set()

        # W4-02: Circuit breaker — configured via settings, defaults provided
        from engine.config.settings import settings

        self._circuit_breaker = CircuitBreaker(
            name="neo4j",
            failure_threshold=settings.neo4j_circuit_threshold,
            recovery_timeout=settings.neo4j_circuit_cooldown,
            half_open_max_calls=settings.neo4j_circuit_half_open_max,
        )

    async def connect(self) -> None:
        """Establish driver connection."""
        async with self._lock:
            if self._driver:
                logger.warning("Driver already connected")
                return

            logger.info(f"Connecting to Neo4j at {self.uri}")
            self._driver = AsyncGraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                max_connection_lifetime=3600,
                max_connection_pool_size=50,
                connection_acquisition_timeout=60,
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
        """Execute Cypher query without circuit breaker (internal)."""
        driver = self.get_driver()

        async with driver.session(database=database) as session:
            result = await session.run(cypher, parameters or {})
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
        if database is None:
            from engine.config.settings import settings

            if settings.strict_tenant_database:
                msg = (
                    "GraphDriver.execute_query requires an explicit database (W7-01); implicit 'neo4j' fallback removed"
                )
                raise ValueError(msg)
            database = "neo4j"

        # CEG-008: a tenant domain database has to exist before it can be
        # queried. Under the flag we provision it on first use; without it we at
        # least say what is missing instead of surfacing the driver's message.
        from engine.config.settings import settings as _db_settings

        if _db_settings.auto_create_domain_database:
            await self.ensure_database(database)

        try:
            return await self._circuit_breaker.call(self._raw_execute_query, cypher, parameters, database)
        except Exception as exc:
            if _looks_like_absent_database(exc) and database not in _BUILTIN_DATABASES:
                msg = (
                    f"Neo4j database {database!r} does not exist. Domain queries route to a "
                    f"database named after the domain id, and Neo4j does not create one "
                    f"implicitly. Run this against the system database (Enterprise "
                    f"Edition):  CREATE DATABASE `{database}` IF NOT EXISTS WAIT  "
                    f"-- or set AUTO_CREATE_DOMAIN_DATABASE=true to have the engine "
                    f"create it on first use."
                )
                raise DatabaseNotProvisionedError(msg) from exc
            raise

    async def ensure_database(self, name: str) -> bool:
        """Create the domain database if it is absent. Idempotent per process.

        Returns True when the database is known to exist afterwards, False when
        provisioning was not possible (Community Edition, or insufficient
        privileges) — in which case the query that follows fails with the
        message above rather than here, so a read-only deployment that has the
        database already is not blocked by a CREATE it is not allowed to run.
        """
        if name in _BUILTIN_DATABASES or name in self._ensured_databases:
            return True
        if not _DATABASE_NAME_RE.fullmatch(name):
            msg = (
                f"refusing to provision Neo4j database {name!r}: a database name must begin "
                f"with a letter and contain only letters, digits, dots, dashes or underscores "
                f"(3-63 characters). CREATE DATABASE takes no query parameter, so an "
                f"unvalidated name would be interpolated into an administrative command."
            )
            raise ValueError(msg)

        # Claim the name BEFORE the await, not after. `_raw_execute_query` is a
        # suspension point: with the add afterwards, every request that arrived
        # while the first CREATE was in flight passed the membership check above
        # and issued its own administrative command — "at most once per database
        # per process" held only when calls did not overlap, which is exactly
        # when it does not matter. The claim is released on failure so a later
        # attempt (different privileges, Enterprise now licensed) can retry.
        self._ensured_databases.add(name)

        # Administrative commands must run against `system`, and the name is
        # back-quoted because a dash is legal in a database name but not in a
        # bare identifier. The regex above is what makes that quoting safe.
        cypher = f"CREATE DATABASE `{name}` IF NOT EXISTS WAIT"
        try:
            await self._raw_execute_query(cypher, None, "system")
        except Exception as exc:
            self._ensured_databases.discard(name)
            logger.warning(
                "Could not provision Neo4j database %r (%s: %s). "
                "CREATE DATABASE is Enterprise Edition only and requires admin privileges.",
                name,
                type(exc).__name__,
                exc,
            )
            return False

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

        async with driver.session(database=database) as session:
            if transaction_function is not None:
                return await session.execute_write(transaction_function, *args, **kwargs)

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

            return await session.execute_write(_run_statement)

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
        """
        if database is None:
            from engine.config.settings import settings

            if settings.strict_tenant_database:
                msg = (
                    "GraphDriver.execute_write requires an explicit database (W7-01); implicit 'neo4j' fallback removed"
                )
                raise ValueError(msg)
            database = "neo4j"
        return await self._circuit_breaker.call(
            self._raw_execute_write,
            transaction_function,
            *args,
            cypher=cypher,
            parameters=parameters,
            database=database,
            **kwargs,
        )
