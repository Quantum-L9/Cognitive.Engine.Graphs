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

Tests for Wave 7 W7-01: Explicit Tenant Database Binding (EVID-003 / UNK-002).

The silent ``database: str = "neo4j"`` default on GraphDriver.execute_query /
execute_write let a caller that omits ``database=`` land on the shared "neo4j"
database instead of the tenant's database. When ``strict_tenant_database`` is on,
omitting ``database=`` must fail loudly; when off (default), it falls back to
"neo4j" for backward compatibility.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from engine.config.settings import Settings
from engine.graph.driver import GraphDriver


def _settings(*, strict: bool) -> Settings:
    return Settings(
        neo4j_password="test-pw",
        api_secret_key="test-key",
        strict_tenant_database=strict,
    )


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


@pytest.mark.finding("EVID-003")
@pytest.mark.asyncio
async def test_execute_query_defaults_to_neo4j_when_not_strict() -> None:
    """Back-compat: flag off → implicit 'neo4j' fallback, no real Neo4j needed."""
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
    """An explicit database= (as the internal health probe passes) stays valid."""
    driver = GraphDriver()
    driver._raw_execute_query = AsyncMock(return_value=[])  # type: ignore[method-assign]
    with patch("engine.config.settings.settings", _settings(strict=True)):
        await driver.execute_query("RETURN 1", database="neo4j")
    assert driver._raw_execute_query.await_args.args[2] == "neo4j"
