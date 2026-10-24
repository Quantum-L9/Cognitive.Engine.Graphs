"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, wave7, graph, driver, tenancy, sel4]
owner: engine-team
status: active
--- /L9_META ---

Tests for Wave 7 W7-01: Explicit Tenant Database Binding (EVID-003 / UNK-002),
and DB-STRICT-01, which flipped the default.

The silent ``database: str = "neo4j"`` default on GraphDriver.execute_query /
execute_write let a caller that omits ``database=`` land on the shared "neo4j"
database instead of the tenant's database. ``strict_tenant_database`` now ships
True: omitting ``database=`` fails loudly by default; ``STRICT_TENANT_DATABASE=false``
restores the "neo4j" fallback for a deployment that still has an unaudited caller.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from engine.config.settings import Settings
from engine.graph.driver import GraphDriver


def _settings(*, strict: bool | None = None) -> Settings:
    overrides = {} if strict is None else {"strict_tenant_database": strict}
    return Settings(neo4j_password="test-pw", api_secret_key="test-key", **overrides)


@pytest.mark.finding("DB-STRICT-01")
def test_strict_tenant_database_is_on_by_default() -> None:
    assert _settings().strict_tenant_database is True


@pytest.mark.finding("DB-STRICT-01")
@pytest.mark.asyncio
async def test_execute_query_without_database_raises_by_default() -> None:
    """No flag override at all: the shipped default refuses an omitted database=."""
    driver = GraphDriver()
    driver._raw_execute_query = AsyncMock(return_value=[])  # type: ignore[method-assign]
    with patch("engine.config.settings.settings", _settings()):
        with pytest.raises(ValueError, match="explicit database"):
            await driver.execute_query("RETURN 1")
    driver._raw_execute_query.assert_not_awaited()


@pytest.mark.finding("EVID-003")
@pytest.mark.asyncio
async def test_execute_query_without_database_raises_when_strict() -> None:
    driver = GraphDriver()
    with patch("engine.config.settings.settings", _settings(strict=True)):
        with pytest.raises(ValueError, match="explicit database"):
            await driver.execute_query("RETURN 1")


@pytest.mark.finding("EVID-003")
@pytest.mark.asyncio
async def test_execute_write_without_database_raises_when_strict() -> None:
    driver = GraphDriver()
    with patch("engine.config.settings.settings", _settings(strict=True)):
        with pytest.raises(ValueError, match="explicit database"):
            await driver.execute_write(cypher="CREATE (n)")


@pytest.mark.finding("DB-STRICT-01")
@pytest.mark.asyncio
async def test_error_names_the_method_and_the_opt_out() -> None:
    driver = GraphDriver()
    with patch("engine.config.settings.settings", _settings()):
        with pytest.raises(ValueError, match=r"execute_write .*STRICT_TENANT_DATABASE=false"):
            await driver.execute_write(cypher="CREATE (n)")


@pytest.mark.finding("EVID-003")
@pytest.mark.asyncio
async def test_execute_query_defaults_to_neo4j_when_not_strict() -> None:
    """Back-compat: flag explicitly off → implicit 'neo4j' fallback, no real Neo4j needed."""
    driver = GraphDriver()
    # Stub the raw executor so no live driver/connection is required, and capture
    # the resolved database argument the public method forwarded.
    driver._raw_execute_query = AsyncMock(return_value=[{"ping": 1}])  # type: ignore[method-assign]
    with patch("engine.config.settings.settings", _settings(strict=False)):
        result = await driver.execute_query("RETURN 1 AS ping")
    assert result == [{"ping": 1}]
    # Positional args to _raw_execute_query: (cypher, parameters, database)
    assert driver._raw_execute_query.await_args.args[2] == "neo4j"


@pytest.mark.finding("EVID-003")
@pytest.mark.asyncio
async def test_explicit_database_is_preserved_when_strict() -> None:
    """An explicit database= (as handle_health's probe passes) stays valid under the default."""
    driver = GraphDriver()
    driver._raw_execute_query = AsyncMock(return_value=[])  # type: ignore[method-assign]
    with patch("engine.config.settings.settings", _settings()):
        await driver.execute_query("RETURN 1", database="neo4j")
    assert driver._raw_execute_query.await_args.args[2] == "neo4j"
