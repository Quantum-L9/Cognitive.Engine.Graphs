"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, multi-tenant]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_multi_tenant.py
Multi-tenant isolation, the way production isolates (Contract 3).

A tenant resolves to its own domain pack, and every handler query runs in that
pack's database (``database=domain_spec.domain.id``). Isolation is therefore a
property of the engine's routing, not of a ``tenant`` property a test writes by
hand. Two tenants here are two copies of the synthetic orbital-logistics pack
under different domain ids, so labels, ids and Cypher are identical and only the
database differs: a leak cannot hide behind a label mismatch.

Every graph state is written by ``handle_sync`` and read by ``handle_match``;
raw Cypher only provisions and wipes the two databases.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.integration

ALPHA = "orbital-alpha"
BETA = "orbital-beta"
DIRECTION = "window_to_depot"


def _write_tenant_packs(root: Path) -> None:
    """Two copies of orbital-logistics whose only difference is the domain id (and so the database)."""
    source = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "orbital-logistics" / "spec.yaml").read_text())
    for tenant in (ALPHA, BETA):
        raw = dict(source)
        raw["domain"] = {**source["domain"], "id": tenant}
        (root / tenant).mkdir()
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))


@pytest_asyncio.fixture
async def two_tenants(graph_driver, tmp_path: Path) -> AsyncGenerator[None, None]:
    from engine.config.loader import DomainPackLoader
    from engine.handlers import init_dependencies
    from engine.state import get_state

    _write_tenant_packs(tmp_path)
    for tenant in (ALPHA, BETA):
        await graph_driver.execute_query(
            cypher=f"CREATE DATABASE `{tenant}` IF NOT EXISTS WAIT", parameters={}, database="system"
        )
    get_state().reset()
    init_dependencies(graph_driver, DomainPackLoader(config_path=str(tmp_path)))
    yield
    get_state().reset()
    for tenant in (ALPHA, BETA):
        await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=tenant)


def _depot(depot_id: str, reliability: float) -> dict[str, Any]:
    return {"depot_id": depot_id, "accepts_hazmat": True, "docking_ports": 4, "reliability": reliability}


async def _sync(tenant: str, batch: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(tenant, {"entity_type": "orbital-depots", "batch": batch})
    assert result["status"] == "success"


def _by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate"]["depot_id"]: row["candidate"] for row in result.get("candidates", [])}


async def _seed() -> None:
    # The same depot id in both tenants, with different values, plus one depot each.
    await _sync(ALPHA, [_depot("shared", 0.91), _depot("alpha-only", 0.80)])
    await _sync(BETA, [_depot("shared", 0.42), _depot("beta-only", 0.70)])


def _legacy(**extra: Any) -> dict[str, Any]:
    return {"query": {"min_ports": 2}, "match_direction": DIRECTION, "top_n": 25, **extra}


async def test_each_tenant_matches_only_its_own_synced_candidates(two_tenants):
    from engine.handlers import handle_match

    await _seed()

    alpha = _by_id(await handle_match(ALPHA, _legacy()))
    beta = _by_id(await handle_match(BETA, _legacy()))

    assert set(alpha) == {"shared", "alpha-only"}
    assert set(beta) == {"shared", "beta-only"}
    # Same id, different tenants: each sees its own write, never the other's.
    assert alpha["shared"]["reliability"] == pytest.approx(0.91)
    assert beta["shared"]["reliability"] == pytest.approx(0.42)
    # handle_sync stamps the writing tenant on every node it writes.
    assert {c["_tenant"] for c in alpha.values()} == {ALPHA}
    assert {c["_tenant"] for c in beta.values()} == {BETA}


async def test_a_sync_into_one_tenant_never_reaches_the_other(two_tenants):
    from engine.handlers import handle_match

    await _seed()
    await _sync(ALPHA, [_depot("late-alpha", 0.99)])

    assert "late-alpha" in _by_id(await handle_match(ALPHA, _legacy()))
    assert set(_by_id(await handle_match(BETA, _legacy()))) == {"shared", "beta-only"}


@pytest.mark.parametrize("key", ["tenant", "domain", "domain_id", "database"])
async def test_a_legacy_payload_naming_the_other_tenant_cannot_redirect_the_query(two_tenants, key):
    from engine.handlers import handle_match

    await _seed()

    alpha = _by_id(await handle_match(ALPHA, _legacy(**{key: BETA})))

    assert set(alpha) == {"shared", "alpha-only"}
    assert alpha["shared"]["reliability"] == pytest.approx(0.91)


def _match_request(domain: str) -> dict[str, Any]:
    return {
        "contract_version": "1.0.0-draft",
        "domain": domain,
        "query_id": "isolation-1",
        "direction": DIRECTION,
        "query_entity_ref": "orbital.launch.window:1",
        "query": {"facts": {"min_ports": 2}, "evidence_summary": []},
        "top_n": 25,
        "projection_version": "1.0.0",
        "policy_ref": {"id": "orbital.match-policy", "version": "1.0.0"},
    }


async def test_a_contract_request_naming_the_other_domain_is_refused(two_tenants):
    from engine.handlers import ValidationError, handle_match

    await _seed()
    assert set(_by_id(await handle_match(ALPHA, _match_request(ALPHA)))) == {"shared", "alpha-only"}
    smuggled = _match_request(BETA)
    with pytest.raises(ValidationError, match="is not the admitted domain"):
        await handle_match(ALPHA, smuggled)
