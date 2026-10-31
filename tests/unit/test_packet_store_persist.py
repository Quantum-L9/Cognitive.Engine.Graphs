"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, packet-store, persistence, audit]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_packet_store_persist.py
Unit tests for PacketStore.persist() against a fake asyncpg pool/connection.

The fake records every statement with its bound arguments and whether it ran
inside ``conn.transaction()``, and returns asyncpg-style status strings, so
the tests can assert on the SQL PacketStore issues without a PostgreSQL.

Coverage:
  - RU-078 / RG-101: content_hash is the packet identity (transport_hash),
    the packet INSERT skips on ``ON CONFLICT (packet_id)`` (so a re-delivery
    of a row stored under the legacy payload_hash key is skipped rather than
    raising on the primary key), and a packet INSERT reported as skipped
    ('INSERT 0 0') writes no hop_trace, delegation_chain or lineage_graph
    rows for that packet.
  - RU-079 / RG-103: a transaction-local
    ``set_config('app.current_tenant', <tenant.actor>, true)`` precedes each
    packet INSERT inside the persist transaction.
  - Flags off: the legacy payload_hash key, unconditional child INSERTs and
    no set_config statement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

import engine.packet.packet_store as ps_mod
from engine.config.settings import settings
from engine.packet.chassis_contract import deflate_egress, inflate_ingress

# Positional index of content_hash in _INSERT_PACKET_SQL's bound arguments.
_CONTENT_HASH_ARG = 13

# -- Fake asyncpg ------------------------------------------------------------


def _is_packet_insert(sql: str) -> bool:
    return sql is ps_mod._INSERT_PACKET_SQL or sql is ps_mod._INSERT_PACKET_IDENTITY_SQL


@dataclass
class _Call:
    method: str
    sql: str
    args: tuple[Any, ...]
    in_transaction: bool


@dataclass
class _FakeConn:
    """Records statements; ``insert_status`` decides each packet INSERT's status."""

    insert_status: Callable[[tuple[Any, ...]], str] = lambda _args: "INSERT 0 1"
    calls: list[_Call] = field(default_factory=list)
    _tx_depth: int = 0

    def _record(self, method: str, sql: str, args: tuple[Any, ...]) -> None:
        self.calls.append(_Call(method, sql, args, self._tx_depth > 0))

    async def execute(self, sql: str, *args: Any) -> str:
        self._record("execute", sql, args)
        if _is_packet_insert(sql):
            return self.insert_status(args)
        if sql.lstrip().startswith("UPDATE"):
            return "UPDATE 1"
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._record("fetchval", sql, args)
        return args[0] if args else None

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    # helpers
    def statements(self, sql: str) -> list[_Call]:
        return [c for c in self.calls if c.sql is sql]


class _FakeTransaction:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeTransaction:
        self._conn._tx_depth += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._conn._tx_depth -= 1
        return False


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _FakePoolManager:
    def __init__(self, conn: _FakeConn) -> None:
        self._pool = _FakePool(conn)

    async def get_pool(self) -> _FakePool:
        return self._pool


def _install(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    monkeypatch.setattr(ps_mod, "_PACKET_STORE_ENABLED", True)
    monkeypatch.setattr(ps_mod, "_pool_manager", _FakePoolManager(conn))


# -- Packets -----------------------------------------------------------------


def _pair(payload: dict[str, Any], tenant: str = "tenant-a", trace_id: str = "tr") -> tuple[Any, Any]:
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


# -- RU-078 / RG-101 ---------------------------------------------------------


@pytest.mark.unit
class TestPacketIdentityDedup:
    async def test_identical_payload_requests_get_distinct_content_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeConn()
        _install(monkeypatch, conn)
        store = ps_mod.PacketStore()

        req1, resp1 = _pair({"query": {"x": 1}}, trace_id="tr-1")
        req2, resp2 = _pair({"query": {"x": 1}}, trace_id="tr-2")
        assert req1.security.payload_hash == req2.security.payload_hash  # precondition

        await store.persist(req1, resp1)
        await store.persist(req2, resp2)

        hashes = [c.args[_CONTENT_HASH_ARG] for c in conn.statements(ps_mod._INSERT_PACKET_IDENTITY_SQL)]
        assert hashes == [
            req1.security.transport_hash,
            resp1.security.transport_hash,
            req2.security.transport_hash,
            resp2.security.transport_hash,
        ]
        assert hashes[0] != hashes[2]
        assert len(set(hashes)) == 4

    async def test_skipped_packet_insert_writes_no_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both packets of the pair already exist: every packet INSERT is skipped.
        conn = _FakeConn(insert_status=lambda _args: "INSERT 0 0")
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}})
        assert len(resp.hop_trace) == 1  # precondition: the response carries a hop
        assert resp.lineage.parent_id == req.header.packet_id

        await ps_mod.PacketStore().persist(req, resp)

        assert len(conn.statements(ps_mod._INSERT_PACKET_IDENTITY_SQL)) == 2
        assert conn.statements(ps_mod._INSERT_HOP_SQL) == []
        assert conn.statements(ps_mod._INSERT_DELEGATION_SQL) == []
        assert conn.statements(ps_mod._INSERT_LINEAGE_SQL) == []

    async def test_only_the_skipped_packet_loses_its_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, resp = _pair({"query": {"x": 1}})
        req_id = str(req.header.packet_id)

        # The request is a re-delivery (already stored); the response is new.
        conn = _FakeConn(insert_status=lambda args: "INSERT 0 0" if args[0] == req_id else "INSERT 0 1")
        _install(monkeypatch, conn)

        await ps_mod.PacketStore().persist(req, resp)

        hops = conn.statements(ps_mod._INSERT_HOP_SQL)
        assert [c.args[0] for c in hops] == [str(resp.header.packet_id)] * len(resp.hop_trace)
        lineage = conn.statements(ps_mod._INSERT_LINEAGE_SQL)
        assert [(c.args[0], c.args[1]) for c in lineage] == [(req_id, str(resp.header.packet_id))]

    async def test_repersisting_same_pair_does_not_duplicate_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stored: set[tuple[Any, Any]] = set()

        def status(args: tuple[Any, ...]) -> str:
            # Model the packet_id primary key ... ON CONFLICT (packet_id) DO NOTHING.
            key = args[0]
            if key in stored:
                return "INSERT 0 0"
            stored.add(key)
            return "INSERT 0 1"

        conn = _FakeConn(insert_status=status)
        _install(monkeypatch, conn)
        store = ps_mod.PacketStore()
        req, resp = _pair({"query": {"x": 1}})

        await store.persist(req, resp)
        await store.persist(req, resp)

        assert len(conn.statements(ps_mod._INSERT_HOP_SQL)) == len(req.hop_trace) + len(resp.hop_trace)
        assert len(conn.statements(ps_mod._INSERT_LINEAGE_SQL)) == 1

    async def test_flag_on_insert_skips_on_packet_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeConn()
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}})

        await ps_mod.PacketStore().persist(req, resp)

        inserts = [c for c in conn.calls if _is_packet_insert(c.sql)]
        assert len(inserts) == 2
        for call in inserts:
            assert call.sql is ps_mod._INSERT_PACKET_IDENTITY_SQL
            assert call.sql.rstrip().endswith("ON CONFLICT (packet_id) DO NOTHING")

    async def test_all_writes_share_one_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeConn()
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}})

        await ps_mod.PacketStore().persist(req, resp)

        assert conn.calls
        assert all(c.in_transaction for c in conn.calls)


@pytest.mark.unit
class TestPacketIdentityDedupFlagOff:
    async def test_flag_off_keys_on_payload_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "packet_store_packet_identity_dedup_enabled", False)
        conn = _FakeConn()
        _install(monkeypatch, conn)
        req1, resp1 = _pair({"query": {"x": 1}}, trace_id="tr-1")
        req2, resp2 = _pair({"query": {"x": 1}}, trace_id="tr-2")

        store = ps_mod.PacketStore()
        await store.persist(req1, resp1)
        await store.persist(req2, resp2)

        hashes = [c.args[_CONTENT_HASH_ARG] for c in conn.statements(ps_mod._INSERT_PACKET_SQL)]
        assert hashes == [
            req1.security.payload_hash,
            resp1.security.payload_hash,
            req2.security.payload_hash,
            resp2.security.payload_hash,
        ]
        assert hashes[0] == hashes[2]
        assert all(not _is_packet_insert(c.sql) or c.sql is ps_mod._INSERT_PACKET_SQL for c in conn.calls)
        assert ps_mod._INSERT_PACKET_SQL.rstrip().endswith("ON CONFLICT (content_hash, actor_tenant) DO NOTHING")

    async def test_flag_off_writes_children_after_skipped_insert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "packet_store_packet_identity_dedup_enabled", False)
        conn = _FakeConn(insert_status=lambda _args: "INSERT 0 0")
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}})

        await ps_mod.PacketStore().persist(req, resp)

        assert len(conn.statements(ps_mod._INSERT_HOP_SQL)) == len(req.hop_trace) + len(resp.hop_trace)
        assert len(conn.statements(ps_mod._INSERT_LINEAGE_SQL)) == 1


# -- RU-079 / RG-103 ---------------------------------------------------------


def _is_set_tenant(call: _Call) -> bool:
    return "set_config('app.current_tenant'" in call.sql


@pytest.mark.unit
class TestRlsTenantSession:
    async def test_set_config_precedes_each_packet_insert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeConn()
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}}, tenant="tenant-rls")

        await ps_mod.PacketStore().persist(req, resp)

        inserts = [i for i, c in enumerate(conn.calls) if _is_packet_insert(c.sql)]
        assert len(inserts) == 2
        for index, packet in zip(inserts, (req, resp), strict=True):
            previous = conn.calls[index - 1]
            assert _is_set_tenant(previous)
            assert previous.args == (packet.tenant.actor,)
            assert previous.in_transaction
        assert sum(_is_set_tenant(c) for c in conn.calls) == 2

    async def test_set_config_is_transaction_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeConn()
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}})

        await ps_mod.PacketStore().persist(req, resp)

        (sql,) = {c.sql for c in conn.calls if _is_set_tenant(c)}
        # is_local=true: the GUC ends with the transaction, so a pooled
        # connection never carries one tenant's setting into another's write.
        assert sql.strip() == "SELECT set_config('app.current_tenant', $1, true)"

    async def test_flag_off_issues_no_set_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "packet_store_rls_tenant_session_enabled", False)
        conn = _FakeConn()
        _install(monkeypatch, conn)
        req, resp = _pair({"query": {"x": 1}})

        await ps_mod.PacketStore().persist(req, resp)

        assert not any(_is_set_tenant(c) for c in conn.calls)
        assert _is_packet_insert(conn.calls[0].sql)
