"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, packet-store, postgres, audit]
owner: engine-team
status: active
--- /L9_META ---

tests/integration/test_packet_store_pg.py
PacketStore.persist against a real PostgreSQL with the shipped
engine/packet/packet_store.sql applied.

Requires ``PACKET_STORE_TEST_DSN``: a superuser DSN for a PostgreSQL 16
server, e.g. ``postgresql://postgres@127.0.0.1:5432/postgres``. Each test
module creates a throw-away database, applies the DDL, and drops it again.
The repository has no PostgreSQL test fixture (the testcontainers extra is
neo4j only), so without that variable the module is skipped, the same way
the Neo4j fixture skips without ``NEO4J_TEST_URI`` (R4-DEC-05 decides whether
a PostgreSQL container joins CI).

Scenarios (RG-102):
  A. Two requests with identical payloads from one tenant: both pairs stored.
  B. The same pair persisted twice: hop_trace keeps one row per hop.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

import engine.packet.packet_store as ps_mod
from engine.config.settings import settings
from engine.packet.chassis_contract import deflate_egress, inflate_ingress

asyncpg = pytest.importorskip("asyncpg")

_ADMIN_DSN = os.getenv("PACKET_STORE_TEST_DSN")
_DDL = (Path(__file__).resolve().parents[2] / "engine" / "packet" / "packet_store.sql").read_text()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ADMIN_DSN, reason="PACKET_STORE_TEST_DSN not set (no PostgreSQL test server)"),
]


def _with_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _with_user(dsn: str, user: str, password: str) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    netloc = f"{user}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="module")
async def packet_db() -> AsyncIterator[str]:
    """Create a fresh database with packet_store.sql applied; yield its owner DSN."""
    assert _ADMIN_DSN is not None
    name = f"ceg_packet_store_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = _with_database(_ADMIN_DSN, name)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_DDL)
    finally:
        await conn.close()
    yield dsn
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def _store_for(monkeypatch: pytest.MonkeyPatch, dsn: str) -> AsyncIterator[ps_mod.PacketStore]:
    monkeypatch.setattr(ps_mod, "_PACKET_STORE_ENABLED", True)
    monkeypatch.setattr(settings, "packet_store_dsn", dsn)
    monkeypatch.setattr(settings, "packet_store_pool_min", 1)
    monkeypatch.setattr(settings, "packet_store_pool_max", 2)
    manager = ps_mod._PoolManager()
    monkeypatch.setattr(ps_mod, "_pool_manager", manager)
    try:
        yield ps_mod.PacketStore()
    finally:
        await manager.close()


@pytest.fixture
async def owner_store(monkeypatch: pytest.MonkeyPatch, packet_db: str) -> AsyncIterator[ps_mod.PacketStore]:
    """PacketStore connected as the database owner (RLS not enforced)."""
    async for store in _store_for(monkeypatch, packet_db):
        yield store


def _pair(payload: dict[str, Any], tenant: str, trace_id: str) -> tuple[Any, Any]:
    request = inflate_ingress(action="match", payload=payload, tenant=tenant, trace_id=trace_id)
    response = deflate_egress(
        request=request,
        engine_data={"ok": True},
        status="success",
        processing_ms=1.0,
        engine_version="1.1.0",
        responding_node="graph-engine",
    )
    return request, response


async def _count(dsn: str, sql: str, *args: Any) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return int(await conn.fetchval(sql, *args))
    finally:
        await conn.close()


async def test_identical_payload_pairs_are_all_stored(owner_store: ps_mod.PacketStore, packet_db: str) -> None:
    """Scenario A: before RU-078 pair 2 raised ForeignKeyViolation and rolled back."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    req1, resp1 = _pair({"query": {"x": 1}}, tenant, "tr-a1")
    req2, resp2 = _pair({"query": {"x": 1}}, tenant, "tr-a2")

    await owner_store.persist(req1, resp1)
    await owner_store.persist(req2, resp2)

    assert await _count(packet_db, "SELECT count(*) FROM packet_store WHERE actor_tenant = $1", tenant) == 4
    assert (
        await _count(
            packet_db,
            "SELECT count(*) FROM lineage_graph WHERE child_id = ANY($1::uuid[])",
            [str(resp1.header.packet_id), str(resp2.header.packet_id)],
        )
        == 2
    )


async def test_repersisted_pair_keeps_one_hop_row_per_hop(owner_store: ps_mod.PacketStore, packet_db: str) -> None:
    """Scenario B: before RU-078 hop_trace held 2 rows for a packet carrying 1 hop."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    req, resp = _pair({"query": {"x": 2}}, tenant, "tr-b")

    await owner_store.persist(req, resp)
    await owner_store.persist(req, resp)

    for packet in (req, resp):
        rows = await _count(
            packet_db, "SELECT count(*) FROM hop_trace WHERE packet_id = $1::uuid", str(packet.header.packet_id)
        )
        assert rows == len(packet.hop_trace)
    assert await _count(packet_db, "SELECT count(*) FROM packet_store WHERE actor_tenant = $1", tenant) == 2
