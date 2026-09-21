"""CEG-008 — a domain database that does not exist must say so, or be created.

`match` and `sync` route queries to a Neo4j database named after the domain id
(`engine/handlers.py`, `database=domain_spec.domain.id`). Neo4j does not create
databases implicitly, so on a fresh instance every sync and match failed with
the driver's own message until an operator ran CREATE DATABASE by hand — the
Constellation E2E hit exactly this and had to add the step to its boot sequence.

Two halves, both tested here: with `auto_create_domain_database` on the engine
provisions on first use; with it off the failure names the missing database and
the exact command that provides it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.graph.driver import (
    DatabaseNotProvisionedError,
    GraphDriver,
    _looks_like_absent_database,
)


class _RecordingDriver(GraphDriver):
    """GraphDriver with the Neo4j round trip replaced by a recorder."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        super().__init__(uri="bolt://unused", username="u", password="p")
        self.calls: list[tuple[str, str]] = []
        self._fail_with = fail_with

    async def _raw_execute_write(  # type: ignore[override]
        self,
        transaction_function: Any = None,
        *args: Any,
        cypher: str | None = None,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((f"WRITE {cypher}", database))
        if self._fail_with is not None and database != "system":
            raise self._fail_with
        return {"nodes_created": 0}

    async def _raw_execute_query(  # type: ignore[override]
        self,
        cypher: str,
        parameters: dict[str, Any] | None = None,
        database: str = "neo4j",
    ) -> list[Any]:
        self.calls.append((cypher, database))
        if self._fail_with is not None and database != "system":
            raise self._fail_with
        return []


@pytest.fixture
def auto_create(monkeypatch):
    from engine.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "auto_create_domain_database", True)


@pytest.fixture
def no_auto_create(monkeypatch):
    from engine.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "auto_create_domain_database", False)


# ── Off: the failure has to be actionable ───────────────────────────────────


def test_absent_database_is_recognised() -> None:
    assert _looks_like_absent_database(Exception("Database does not exist: plasticos"))
    assert _looks_like_absent_database(Exception("Unable to get a routing table for database"))
    assert not _looks_like_absent_database(Exception("SyntaxError: bad cypher"))


@pytest.mark.asyncio
async def test_missing_database_names_itself_and_the_fix(no_auto_create) -> None:
    driver = _RecordingDriver(fail_with=Exception("Database does not exist: plasticos"))
    with pytest.raises(DatabaseNotProvisionedError) as excinfo:
        await driver.execute_query("MATCH (n) RETURN n", database="plasticos")

    message = str(excinfo.value)
    assert "plasticos" in message
    assert "CREATE DATABASE" in message
    assert "AUTO_CREATE_DOMAIN_DATABASE" in message


@pytest.mark.asyncio
async def test_unrelated_errors_are_not_reinterpreted(no_auto_create) -> None:
    boom = ValueError("SyntaxError: unexpected token")
    driver = _RecordingDriver(fail_with=boom)
    with pytest.raises(ValueError, match="unexpected token"):
        await driver.execute_query("MATCH (n RETURN n", database="plasticos")


@pytest.mark.asyncio
async def test_nothing_is_provisioned_while_the_flag_is_off(no_auto_create) -> None:
    driver = _RecordingDriver()
    await driver.execute_query("MATCH (n) RETURN n", database="plasticos")
    assert not any("CREATE DATABASE" in cypher for cypher, _ in driver.calls)


@pytest.mark.asyncio
async def test_missing_database_on_a_write_names_itself_and_the_fix(no_auto_create) -> None:
    driver = _RecordingDriver(fail_with=Exception("Database does not exist: plasticos"))
    with pytest.raises(DatabaseNotProvisionedError, match="CREATE DATABASE"):
        await driver.execute_write(cypher="MERGE (n:X)", database="plasticos")


# ── On: provision once, on first use ────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_use_write_is_provisioned_too(auto_create) -> None:
    """A fresh deployment's first operation can be a managed write (idea-portfolio
    corpus sync), not a match query; provisioning must not depend on which."""
    driver = _RecordingDriver()
    await driver.execute_write(cypher="MERGE (n:X)", database="plasticos")
    assert driver.calls == [
        ("CREATE DATABASE `plasticos` IF NOT EXISTS WAIT", "system"),
        ("WRITE MERGE (n:X)", "plasticos"),
    ]
    # ...and the query that follows reuses the ensured state.
    await driver.execute_query("MATCH (n) RETURN n", database="plasticos")
    assert sum("CREATE DATABASE" in c for c, _ in driver.calls) == 1


@pytest.mark.asyncio
async def test_database_is_created_on_first_use(auto_create) -> None:
    driver = _RecordingDriver()
    await driver.execute_query("MATCH (n) RETURN n", database="plasticos")

    creates = [(c, db) for c, db in driver.calls if "CREATE DATABASE" in c]
    assert creates, "expected the domain database to be provisioned"
    cypher, target = creates[0]
    assert cypher == "CREATE DATABASE `plasticos` IF NOT EXISTS WAIT"
    assert target == "system", "administrative commands must run against `system`"


@pytest.mark.asyncio
async def test_creation_is_attempted_only_once_per_process(auto_create) -> None:
    driver = _RecordingDriver()
    for _ in range(3):
        await driver.execute_query("MATCH (n) RETURN n", database="plasticos")
    assert sum("CREATE DATABASE" in c for c, _ in driver.calls) == 1


@pytest.mark.asyncio
async def test_builtin_databases_are_never_provisioned(auto_create) -> None:
    driver = _RecordingDriver()
    await driver.execute_query("RETURN 1", database="neo4j")
    await driver.execute_query("SHOW DATABASES", database="system")
    assert not any("CREATE DATABASE" in c for c, _ in driver.calls)


@pytest.mark.asyncio
async def test_a_hyphenated_domain_id_is_accepted() -> None:
    """Domain ids legitimately contain dashes, so sanitize_label cannot be used."""
    driver = _RecordingDriver()
    assert await driver.ensure_database("healthcare-referral") is True
    assert ("CREATE DATABASE `healthcare-referral` IF NOT EXISTS WAIT", "system") in driver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "plasticos`; DROP DATABASE neo4j; --",  # back-quote escape
        "1leading-digit",
        "ab",  # under the 3-character minimum
        "has space",
        "",
    ],
)
async def test_an_unsafe_database_name_is_refused(name: str) -> None:
    """CREATE DATABASE takes no parameter, so the name is interpolated — and
    therefore validated before it ever reaches the statement."""
    driver = _RecordingDriver()
    with pytest.raises(ValueError, match="refusing to provision"):
        await driver.ensure_database(name)
    assert driver.calls == []


class _RefusingDriver(_RecordingDriver):
    """Community Edition: CREATE DATABASE is rejected, ordinary queries work."""

    async def _raw_execute_query(self, cypher, parameters=None, database="neo4j"):
        self.calls.append((cypher, database))
        if "CREATE DATABASE" in cypher:
            msg = "Unsupported administration command in Community Edition"
            raise RuntimeError(msg)
        return []


@pytest.mark.asyncio
async def test_provisioning_failure_is_reported_not_raised(auto_create) -> None:
    """Community Edition rejects CREATE DATABASE. A deployment whose database
    already exists must not be blocked by a CREATE it is not allowed to run."""
    driver = _RefusingDriver()
    assert await driver.ensure_database("plasticos") is False
    # The ordinary query still runs; it is the query that reports the truth.
    assert await driver.execute_query("MATCH (n) RETURN n", database="plasticos") == []


@pytest.mark.asyncio
async def test_failed_provisioning_is_retried_on_the_next_call() -> None:
    driver = _RefusingDriver()
    assert await driver.ensure_database("plasticos") is False
    assert await driver.ensure_database("plasticos") is False
    assert sum("CREATE DATABASE" in c for c, _ in driver.calls) == 2


# ── On: concurrent first use ────────────────────────────────────────────────


class _SlowCreateDriver(_RecordingDriver):
    """The CREATE suspends until the test releases it, like a real `WAIT` would."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.create_started = asyncio.Event()

    async def _raw_execute_query(self, cypher, parameters=None, database="neo4j"):
        self.calls.append((cypher, database))
        if "CREATE DATABASE" in cypher:
            self.create_started.set()
            await self.release.wait()
        return []


@pytest.mark.asyncio
async def test_concurrent_first_use_issues_one_create_and_no_query_runs_before_it_completes(auto_create) -> None:
    """F283-1: a second request arriving while the CREATE is in flight must
    wait for it — not see the name already claimed and race into the domain
    query against a database that does not exist yet."""
    driver = _SlowCreateDriver()
    requests = [asyncio.create_task(driver.execute_query("MATCH (n) RETURN n", database="plasticos")) for _ in range(5)]

    await driver.create_started.wait()
    await asyncio.sleep(0)  # let every request reach its await
    assert driver.calls == [("CREATE DATABASE `plasticos` IF NOT EXISTS WAIT", "system")], (
        "no domain query may run while provisioning is in flight, and only one CREATE may be issued"
    )
    assert all(not task.done() for task in requests)

    driver.release.set()
    await asyncio.gather(*requests)

    creates = [c for c, _ in driver.calls if "CREATE DATABASE" in c]
    queries = [(c, db) for c, db in driver.calls if "CREATE DATABASE" not in c]
    assert len(creates) == 1
    assert queries == [("MATCH (n) RETURN n", "plasticos")] * 5
    # The CREATE precedes every domain query in the recorded order.
    assert driver.calls[0][0].startswith("CREATE DATABASE")


@pytest.mark.asyncio
async def test_concurrent_callers_share_the_provisioning_outcome() -> None:
    driver = _SlowCreateDriver()
    waiters = [asyncio.create_task(driver.ensure_database("plasticos")) for _ in range(3)]
    await driver.create_started.wait()
    driver.release.set()
    assert await asyncio.gather(*waiters) == [True, True, True]
    assert sum("CREATE DATABASE" in c for c, _ in driver.calls) == 1


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_the_create_for_the_others() -> None:
    driver = _SlowCreateDriver()
    first = asyncio.create_task(driver.ensure_database("plasticos"))
    second = asyncio.create_task(driver.ensure_database("plasticos"))
    await driver.create_started.wait()
    await asyncio.sleep(0)

    first.cancel()
    await asyncio.wait([first])
    assert first.cancelled()

    driver.release.set()
    assert await second is True
    assert "plasticos" in driver._ensured_databases


@pytest.mark.asyncio
async def test_in_flight_provisioning_is_forgotten_once_settled() -> None:
    driver = _SlowCreateDriver()
    task = asyncio.create_task(driver.ensure_database("plasticos"))
    await driver.create_started.wait()
    assert "plasticos" in driver._provisioning
    driver.release.set()
    assert await task is True
    await asyncio.sleep(0)  # done callbacks run on the next loop iteration
    assert driver._provisioning == {}
