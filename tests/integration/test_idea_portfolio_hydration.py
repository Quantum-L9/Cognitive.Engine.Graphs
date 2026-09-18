"""Integration tests — idea-portfolio hydration against a real Neo4j.

Closes the concurrency half of audit finding CEG-262-002. The hydrator
serialises revisions with `MERGE (state:IdeaPortfolioHydrationState {state_id})`,
and MERGE is only single-node-safe when a uniqueness constraint covers the merged
property. The unit suite exercises this against a mocked transaction, so it
cannot observe the constraint at all — these tests use a real database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from engine.sync.idea_portfolio import (
    DOMAIN_ID,
    SHOW_CONSTRAINTS_CYPHER,
    STATE_ID_PROPERTY,
    STATE_LABEL,
    IdeaPortfolioHydrationError,
    IdeaPortfolioHydrator,
    compile_hydration_plan,
    state_uniqueness_constraint_present,
)

pytestmark = pytest.mark.integration


def _digest(char: str = "a") -> str:
    return "sha256:" + char * 64


def _envelope(idea_id: str, *, expected: str | None = None, snapshot: str = "b") -> dict[str, Any]:
    return {
        "schema": "ceg.idea-portfolio-hydration/v1",
        "source_snapshot_ref": f"Quantum-L9/IdeaOS@{idea_id}",
        "source_snapshot_digest": _digest(snapshot),
        "expected_graph_revision": expected,
        "records": [
            {
                "schema": "ceg.idea-portfolio-sync-record/v1",
                "operation": "upsert",
                "projection": {
                    "schema": "ideaos.idea-graph-projection/v1",
                    "idea_id": idea_id,
                    "source_refs": [f"Ideas/{idea_id}.md"],
                    "source_digest": _digest(),
                    "lifecycle": {"stage": "expanded", "decision": None, "proof_state": "P1"},
                    "assertions": [
                        {
                            "kind": "capability",
                            "relation": "produces",
                            "key": "shared-capability",
                            "evidence_state": "VERIFIED",
                            "source_refs": [f"Ideas/{idea_id}.md#cap"],
                        }
                    ],
                    "unknowns": [],
                },
            }
        ],
    }


@pytest_asyncio.fixture
async def idea_portfolio_db(graph_driver) -> Any:
    """Create the idea-portfolio database and reset its hydration state per test."""
    await graph_driver.execute_query(
        cypher=f"CREATE DATABASE `{DOMAIN_ID}` IF NOT EXISTS WAIT",
        parameters={},
        database="system",
    )
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=DOMAIN_ID)
    yield graph_driver
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=DOMAIN_ID)


async def _drop_state_constraint(driver: Any) -> None:
    rows = await driver.execute_query(cypher=SHOW_CONSTRAINTS_CYPHER, parameters={}, database=DOMAIN_ID)
    if not state_uniqueness_constraint_present(rows):
        return
    named = await driver.execute_query(
        cypher="SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type, entityType",
        parameters={},
        database=DOMAIN_ID,
    )
    for row in named:
        if STATE_LABEL in (row.get("labelsOrTypes") or []) and list(row.get("properties") or []) == [STATE_ID_PROPERTY]:
            await driver.execute_query(
                cypher=f"DROP CONSTRAINT `{row['name']}` IF EXISTS", parameters={}, database=DOMAIN_ID
            )


async def _create_state_constraint(driver: Any) -> None:
    await driver.execute_query(
        cypher=(
            f"CREATE CONSTRAINT idea_portfolio_state_id IF NOT EXISTS "
            f"FOR (n:{STATE_LABEL}) REQUIRE n.{STATE_ID_PROPERTY} IS UNIQUE"
        ),
        parameters={},
        database=DOMAIN_ID,
    )


async def _count_state_nodes(driver: Any) -> int:
    rows = await driver.execute_query(
        cypher=f"MATCH (s:{STATE_LABEL}) RETURN count(s) AS n", parameters={}, database=DOMAIN_ID
    )
    return int(rows[0]["n"]) if rows else 0


@pytest.mark.asyncio
async def test_show_constraints_predicate_matches_a_real_constraint(idea_portfolio_db) -> None:
    """The predicate agrees with a real server, not just with hand-written rows.

    Guards the uniqueness-constraint rename. Neo4j 5.18 — the server this suite
    pins — reports `type: 'UNIQUENESS'`; later versions report
    `NODE_PROPERTY_UNIQUENESS`. A predicate accepting only one spelling fails
    open: the constraint exists and is not seen, so hydration refuses to run
    forever. Hand-written rows cannot catch that; this test did.
    """
    driver = idea_portfolio_db
    await _drop_state_constraint(driver)
    rows = await driver.execute_query(cypher=SHOW_CONSTRAINTS_CYPHER, parameters={}, database=DOMAIN_ID)
    assert state_uniqueness_constraint_present(rows) is False

    await _create_state_constraint(driver)
    rows = await driver.execute_query(cypher=SHOW_CONSTRAINTS_CYPHER, parameters={}, database=DOMAIN_ID)
    assert state_uniqueness_constraint_present(rows) is True


@pytest.mark.asyncio
async def test_concurrent_initial_hydration_yields_one_state_and_one_revision(idea_portfolio_db) -> None:
    """Two concurrent initial envelopes cannot fork the canonical revision chain.

    Both envelopes declare expected_graph_revision=None, so both are claiming to
    be the first write. With the uniqueness constraint in place exactly one may
    commit; the other must fail rather than create a second canonical state node
    or a second committed child revision.
    """
    driver = idea_portfolio_db
    await _create_state_constraint(driver)

    hydrator = IdeaPortfolioHydrator(driver, enabled=True)
    first = _envelope("idea-alpha", snapshot="b")
    second = _envelope("idea-beta", snapshot="c")

    results = await asyncio.gather(hydrator.apply(first), hydrator.apply(second), return_exceptions=True)

    applied = [r for r in results if isinstance(r, dict)]
    failed = [r for r in results if isinstance(r, BaseException)]

    assert len(applied) == 1, f"expected exactly one commit, got {len(applied)}: {results}"
    assert len(failed) == 1, f"expected exactly one rejection, got {len(failed)}: {results}"
    assert isinstance(failed[0], IdeaPortfolioHydrationError | Exception)

    assert await _count_state_nodes(driver) == 1, "a second canonical state node was created"

    rows = await driver.execute_query(
        cypher=f"MATCH (s:{STATE_LABEL}) RETURN s.current_revision AS rev",
        parameters={},
        database=DOMAIN_ID,
    )
    committed = rows[0]["rev"]
    winner = applied[0]
    assert committed == winner["graph_revision"], "committed revision does not match the winning receipt"
    assert committed in {
        compile_hydration_plan(first).graph_revision,
        compile_hydration_plan(second).graph_revision,
    }


@pytest.mark.asyncio
async def test_replay_of_committed_revision_is_reused_not_reapplied(idea_portfolio_db) -> None:
    """Re-applying the committed envelope is idempotent, not a second revision."""
    driver = idea_portfolio_db
    await _create_state_constraint(driver)
    hydrator = IdeaPortfolioHydrator(driver, enabled=True)

    envelope = _envelope("idea-alpha")
    first = await hydrator.apply(envelope)
    assert first["status"] == "applied"

    replay = await hydrator.apply(envelope)
    assert replay["status"] == "reused"
    assert replay["graph_revision"] == first["graph_revision"]
    assert await _count_state_nodes(driver) == 1


@pytest.mark.asyncio
async def test_wrong_parent_revision_is_rejected(idea_portfolio_db) -> None:
    """A child naming the wrong parent revision is refused, keeping the chain linear."""
    driver = idea_portfolio_db
    await _create_state_constraint(driver)
    hydrator = IdeaPortfolioHydrator(driver, enabled=True)

    await hydrator.apply(_envelope("idea-alpha"))
    with pytest.raises(IdeaPortfolioHydrationError, match="expected parent"):
        await hydrator.apply(_envelope("idea-gamma", expected=_digest("f")))

    assert await _count_state_nodes(driver) == 1
