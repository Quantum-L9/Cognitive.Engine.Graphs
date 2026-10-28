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

Scenarios:
  A. (RG-102) Two requests with identical payloads from one tenant: both
     pairs stored.
  B. (RG-102) The same pair persisted twice: hop_trace keeps one row per hop.
  D. (RG-105) Connected as a login role in l9_product (RLS enforced): persist
     succeeds, and a row whose actor_tenant differs from the session tenant
     is still rejected.
  C. (RG-109, RU-081) record_outcome() for a persisted match request returns
     True as the owner and as an l9_product member; the l9_product UPDATE
     grant covers outcome_jsonb only, and another tenant's row is not updated.
  F. (RU-066 follow-up, PR #352 review) PIIHandler.erase_subject erases the
     requesting tenant's packets for a subject id and nothing of another
     tenant's that reuses the id, as the owner and as an l9_product member.
  E. (RG-107, RU-080) packet_store.sql applied a second time to a populated
     database exits cleanly and keeps every row, foreign key and policy.
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


@pytest.fixture(scope="module")
async def product_dsn(packet_db: str) -> AsyncIterator[str]:
    """A LOGIN role that is a member of l9_product, the DDL's RLS-enforced role."""
    role = f"ceg_app_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    conn = await asyncpg.connect(packet_db)
    try:
        await conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE l9_product")
    finally:
        await conn.close()
    yield _with_user(packet_db, role, password)
    conn = await asyncpg.connect(packet_db)
    try:
        await conn.execute(f"DROP OWNED BY {role}")
        await conn.execute(f"DROP ROLE IF EXISTS {role}")
    finally:
        await conn.close()


@pytest.fixture
async def product_store(monkeypatch: pytest.MonkeyPatch, product_dsn: str) -> AsyncIterator[ps_mod.PacketStore]:
    """PacketStore connected as an l9_product member (RLS enforced)."""
    async for store in _store_for(monkeypatch, product_dsn):
        yield store


async def test_persist_as_l9_product_member(product_store: ps_mod.PacketStore, packet_db: str) -> None:
    """Scenario D: before RU-079 this raised InsufficientPrivilegeError (RLS)."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    req, resp = _pair({"query": {"x": 3}}, tenant, "tr-d")

    await product_store.persist(req, resp)

    assert await _count(packet_db, "SELECT count(*) FROM packet_store WHERE actor_tenant = $1", tenant) == 2
    assert (
        await _count(packet_db, "SELECT count(*) FROM hop_trace WHERE packet_id = $1::uuid", str(resp.header.packet_id))
        == 1
    )


async def test_l9_product_cannot_write_another_tenants_row(product_dsn: str) -> None:
    """The session tenant still bounds what an l9_product connection may insert."""
    req, _resp = _pair({"query": {"x": 4}}, "tenant-other", "tr-d2")
    row = ps_mod._extract_packet_row(req)
    conn = await asyncpg.connect(product_dsn)
    try:
        async with conn.transaction():
            await conn.fetchval(ps_mod._SET_TENANT_SQL, "tenant-session")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(ps_mod._INSERT_PACKET_SQL, *row)
    finally:
        await conn.close()


async def _fresh_database() -> tuple[str, str]:
    assert _ADMIN_DSN is not None
    name = f"ceg_packet_store_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return name, _with_database(_ADMIN_DSN, name)


async def _drop_database(name: str) -> None:
    assert _ADMIN_DSN is not None
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def test_reapplying_ddl_preserves_rows() -> None:
    """Scenario E: before RU-080 the second apply dropped packet_store and
    packet_audit_log, then aborted with 'relation "hop_trace" already exists'."""
    name, dsn = await _fresh_database()
    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_DDL)
            await conn.execute(
                "INSERT INTO packet_store (packet_type, action, source_node, actor_tenant,"
                " originator_tenant, envelope, content_hash, trace_id)"
                " VALUES ('request', 'match', 'n', 'acme', 'acme', '{}', 'h-reapply', 't-reapply')"
            )
            await conn.execute(
                "INSERT INTO packet_audit_log (packet_id, action, actor, tenant)"
                " SELECT packet_id, 'INSERT', 'n', 'acme' FROM packet_store"
            )
            await conn.execute(
                "INSERT INTO hop_trace (packet_id, node_id, action, entered_at, seq)"
                " SELECT packet_id, 'n', 'match', now(), 1 FROM packet_store"
            )
            before = {
                "fks": await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE contype = 'f'"),
                "policies": await conn.fetchval("SELECT count(*) FROM pg_policies"),
            }

            await conn.execute(_DDL)

            for count_sql in (
                "SELECT count(*) FROM packet_store",
                "SELECT count(*) FROM packet_audit_log",
                "SELECT count(*) FROM hop_trace",
            ):
                assert await conn.fetchval(count_sql) == 1, count_sql
            assert await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE contype = 'f'") == before["fks"]
            assert await conn.fetchval("SELECT count(*) FROM pg_policies") == before["policies"]
        finally:
            await conn.close()
    finally:
        await _drop_database(name)


async def _record(store: ps_mod.PacketStore, packet_id: str, tenant: str) -> bool:
    return await store.record_outcome(
        match_packet_id=packet_id,
        tenant=tenant,
        outcome_id=f"out-{uuid.uuid4().hex[:8]}",
        outcome="success",
        match_id="m-1",
        candidate_id="c-1",
        value=1.0,
        feedback_metadata=None,
    )


async def _outcome_of(dsn: str, packet_id: str) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT outcome_jsonb FROM packet_store WHERE packet_id = $1::uuid", packet_id)
    finally:
        await conn.close()


async def test_record_outcome_as_owner(owner_store: ps_mod.PacketStore, packet_db: str) -> None:
    """Scenario C: before RU-081 the UPDATE failed ('column "outcome_jsonb" ... does not exist')."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    req, resp = _pair({"query": {"x": 5}}, tenant, "tr-c1")
    await owner_store.persist(req, resp)
    packet_id = str(req.header.packet_id)

    assert await _record(owner_store, packet_id, tenant) is True
    assert await _outcome_of(packet_db, packet_id) is not None


async def test_record_outcome_as_l9_product_member(product_store: ps_mod.PacketStore, packet_db: str) -> None:
    """Scenario C as the RLS-enforced role: needs the column grant and the UPDATE policy."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    req, resp = _pair({"query": {"x": 6}}, tenant, "tr-c2")
    await product_store.persist(req, resp)
    packet_id = str(req.header.packet_id)

    assert await _record(product_store, packet_id, tenant) is True
    assert await _outcome_of(packet_db, packet_id) is not None
    # Another tenant cannot annotate this row (WHERE actor_tenant + RLS).
    assert await _record(product_store, packet_id, "tenant-other") is False


async def test_l9_product_cannot_update_other_packet_columns(product_dsn: str, packet_db: str) -> None:
    """The column-level grant keeps the envelope and every other column immutable."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    req, _resp = _pair({"query": {"x": 7}}, tenant, "tr-c3")
    conn = await asyncpg.connect(packet_db)
    try:
        await conn.execute(ps_mod._INSERT_PACKET_SQL, *ps_mod._extract_packet_row(req))
    finally:
        await conn.close()
    conn = await asyncpg.connect(product_dsn)
    try:
        async with conn.transaction():
            await conn.fetchval(ps_mod._SET_TENANT_SQL, tenant)
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "UPDATE packet_store SET envelope = '{}'::jsonb WHERE packet_id = $1::uuid",
                    str(req.header.packet_id),
                )
    finally:
        await conn.close()


async def _seed_subject_packets(dsn: str, subject: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for tenant in ("tenant-gdpr-a", "tenant-gdpr-b"):
            await conn.execute(
                "INSERT INTO packet_store (packet_type, action, source_node, actor_tenant,"
                " originator_tenant, envelope, content_hash, trace_id, data_subject_id)"
                " VALUES ('request', 'match', 'n', $1, $1, '{}', $2, 't-gdpr', $3)",
                tenant,
                f"h-{tenant}-{subject}",
                subject,
            )
    finally:
        await conn.close()


async def _tenants_with_subject(dsn: str, subject: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT actor_tenant FROM packet_store WHERE data_subject_id = $1 ORDER BY actor_tenant", subject
        )
        return [r["actor_tenant"] for r in rows]
    finally:
        await conn.close()


@pytest.mark.parametrize("role", ["owner", "l9_product"])
async def test_gdpr_erase_is_tenant_scoped(role: str, packet_db: str, product_dsn: str) -> None:
    """Scenario F: before the fix the owner call deleted both tenants' packets
    (returned 2) and the l9_product call saw no row (returned 0)."""
    from engine.compliance.pii import PIIHandler

    subject = f"subj-{uuid.uuid4().hex[:8]}"
    await _seed_subject_packets(packet_db, subject)
    pool = await asyncpg.create_pool(packet_db if role == "owner" else product_dsn, min_size=1, max_size=1)
    try:
        summary = await PIIHandler().erase_subject(
            subject, db_pool=pool, tenant="tenant-gdpr-a", performed_by="tenant-gdpr-a"
        )
    finally:
        await pool.close()

    assert summary["packets_deleted"] == 1
    assert await _tenants_with_subject(packet_db, subject) == ["tenant-gdpr-b"]
    assert (
        await _count(
            packet_db,
            "SELECT count(*) FROM packet_audit_log WHERE action = 'GDPR_DELETE' AND metadata->>'data_subject_id' = $1",
            subject,
        )
        == 1
    )
