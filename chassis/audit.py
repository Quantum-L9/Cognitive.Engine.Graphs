"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api]
tags: [chassis, audit]
owner: platform-team
status: active
--- /L9_META ---

chassis/audit.py — Engine-Agnostic Audit Logger

Extracted from engine/compliance/audit.py. Zero engine imports.
Every L9 constellation node needs structured audit logging for:
    - SOC2 / HIPAA / GDPR compliance
    - PacketEnvelope governance integration
    - SIEM export (Datadog, Splunk, ELK)
    - PostgreSQL packet_audit_log persistence

The engine-specific ComplianceEngine still lives in engine/compliance/.
This chassis-level logger handles the transport and persistence concerns
that every node shares.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AuditAction(StrEnum):
    """Auditable action categories — extensible per engine."""

    ACCESS = "access"
    MUTATION = "mutation"
    QUERY = "query"
    DELEGATION = "delegation"
    SYNC = "sync"
    ENRICHMENT = "enrichment"
    PII_ACCESS = "pii_access"
    PII_ERASURE = "pii_erasure"
    ADMIN = "admin"
    HEALTH = "health"


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AuditEntry(BaseModel):
    """Immutable audit log entry."""

    model_config = {"frozen": True}

    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action: AuditAction
    severity: AuditSeverity = AuditSeverity.INFO
    actor: str
    tenant: str
    trace_id: str | None = None
    resource: str | None = None
    resource_type: str | None = None
    detail: str | None = None
    payload_hash: str | None = None
    compliance_tags: list[str] = Field(default_factory=list)
    pii_fields_accessed: list[str] = Field(default_factory=list)
    data_subject_id: str | None = None
    outcome: str = "success"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetentionPolicy(BaseModel):
    tag: str
    retention_days: int
    require_encryption: bool = False
    require_immutable_storage: bool = False


@dataclass
class AuditContext:
    """
    Groups compliance and resource metadata for ``AuditLogger.log()``.

    Consolidates the seven optional compliance/resource parameters so the
    ``log()`` method stays within the 13-parameter limit.
    """

    resource: str | None = None
    resource_type: str | None = None
    payload_hash: str | None = None
    compliance_tags: list[str] = dc_field(default_factory=list)
    pii_fields_accessed: list[str] = dc_field(default_factory=list)
    data_subject_id: str | None = None
    metadata: dict[str, Any] = dc_field(default_factory=dict)


DEFAULT_RETENTION: dict[str, RetentionPolicy] = {
    "SOC2": RetentionPolicy(tag="SOC2", retention_days=2555, require_immutable_storage=True),
    "GDPR": RetentionPolicy(tag="GDPR", retention_days=1825, require_encryption=True),
    "HIPAA": RetentionPolicy(tag="HIPAA", retention_days=2190, require_encryption=True, require_immutable_storage=True),
    "ECOA": RetentionPolicy(tag="ECOA", retention_days=730),
}


# ── Pluggable sink protocol ──────────────────────────────────────────


class AuditSink:
    """
    Abstract audit sink. Engines provide concrete implementations.
    E.g. PostgresSink, SIEMSink, CloudWatchSink.
    """

    async def write_batch(self, entries: list[AuditEntry]) -> int:
        """Persist a batch of audit entries. Returns count persisted."""
        raise NotImplementedError


class PostgresSink(AuditSink):
    """
    Concrete audit sink that writes to the packet_audit_log table.

    Uses an asyncpg connection pool for async batch inserts.
    Initialise with an asyncpg pool instance:

        pool = await asyncpg.create_pool(dsn=...)
        sink = PostgresSink(pool)
        audit_logger.add_sink(sink)
    """

    _INSERT_SQL = """
        INSERT INTO packet_audit_log (
            audit_id, timestamp, action, severity, actor, tenant,
            trace_id, resource, resource_type, detail, payload_hash,
            compliance_tags, pii_fields_accessed, data_subject_id,
            outcome, metadata
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
    """

    def __init__(self, db_pool: Any) -> None:
        self._pool = db_pool

    async def write_batch(self, entries: list[AuditEntry]) -> int:
        """Persist a batch of audit entries to packet_audit_log.

        Uses executemany for O(1) round trips. Returns count persisted.
        """
        if not entries:
            return 0

        import json as _json

        batch_data = [
            (
                e.audit_id,
                e.timestamp,
                e.action.value,
                e.severity.value,
                e.actor,
                e.tenant,
                e.trace_id,
                e.resource,
                e.resource_type,
                e.detail,
                e.payload_hash,
                e.compliance_tags,
                e.pii_fields_accessed,
                e.data_subject_id,
                e.outcome,
                _json.dumps(e.metadata, default=str),
            )
            for e in entries
        ]

        async with self._pool.acquire() as conn:
            await conn.executemany(self._INSERT_SQL, batch_data)

        persisted = len(entries)
        logger.info("PostgresSink persisted %d audit entries", persisted)
        return persisted


class LogSink(AuditSink):
    """
    Audit sink that writes entries to the Python logger.
    Useful for development, testing, and as a fallback when no DB is available.
    """

    def __init__(self, log_level: int = logging.INFO) -> None:
        self._log_level = log_level

    async def write_batch(self, entries: list[AuditEntry]) -> int:
        """Write audit entries to the Python logger."""
        for entry in entries:
            logger.log(
                self._log_level,
                "audit_event: action=%s actor=%s tenant=%s outcome=%s",
                entry.action.value,
                entry.actor,
                entry.tenant,
                entry.outcome,
            )
        return len(entries)


class AuditLogger:
    """
    Engine-agnostic structured audit logger.

    Buffers entries in memory and flushes to registered sinks.
    Thread-safe for mixed sync/async usage.

    Delivery semantics (RU-085):

    * One ``threading.Lock`` guards every read, swap and append of the
      buffer and of the per-sink pending lists, so a ``log()`` from a worker
      thread can never land between flush's snapshot and its reset.
    * ``flush()`` moves the buffer into one pending list per sink and
      delivers each sink only its own pending entries. A sink that raises
      keeps its entries pending; the next flush retries that sink alone, so
      a healthy sink receives each ``audit_id`` exactly once. Every sink is
      attempted, then the first sink error is re-raised.
    * ``buffer_size`` bounds the buffer and each sink's pending list. On
      overflow the **oldest** entries are dropped, counted in
      ``dropped_count`` and reported with a WARNING (drop-oldest was chosen
      over a forced flush because ``log()`` is synchronous and may run off
      the event loop, where it cannot await a sink).
    """

    def __init__(
        self,
        retention_policies: dict[str, RetentionPolicy] | None = None,
        buffer_size: int = 100,
        sinks: list[AuditSink] | None = None,
    ):
        self._retention = retention_policies or DEFAULT_RETENTION
        self._buffer: list[AuditEntry] = []
        self._buffer_size = buffer_size
        self._sinks = sinks or []
        # Entries flushed but not yet accepted, one list per sink (same index).
        self._pending: list[list[AuditEntry]] = [[] for _ in self._sinks]
        self._dropped = 0
        self._lock = threading.Lock()
        self._log = logging.getLogger("l9.audit")

    def add_sink(self, sink: AuditSink) -> None:
        """Register an audit persistence sink."""
        with self._lock:
            self._sinks.append(sink)
            self._pending.append([])

    def log(
        self,
        action: AuditAction,
        actor: str,
        tenant: str,
        *,
        severity: AuditSeverity = AuditSeverity.INFO,
        trace_id: str | None = None,
        context: AuditContext | None = None,
        detail: str | None = None,
        outcome: str = "success",
    ) -> AuditEntry:
        """
        Create and buffer an audit entry.

        Parameters
        ----------
        action : AuditAction
        actor : str
        tenant : str
        severity : AuditSeverity
        trace_id : str | None
        context : AuditContext | None
            Consolidates resource, compliance, and PII metadata.
            Pass ``AuditContext(resource=..., compliance_tags=[...])`` as needed.
        detail : str | None
        outcome : str
        """
        ctx = context or AuditContext()
        entry = AuditEntry(
            action=action,
            severity=severity,
            actor=actor,
            tenant=tenant,
            trace_id=trace_id,
            resource=ctx.resource,
            resource_type=ctx.resource_type,
            detail=detail,
            payload_hash=ctx.payload_hash,
            compliance_tags=ctx.compliance_tags,
            pii_fields_accessed=ctx.pii_fields_accessed,
            data_subject_id=ctx.data_subject_id,
            outcome=outcome,
            metadata=ctx.metadata,
        )
        self._emit(entry)
        return entry

    async def flush(self) -> int:
        """Flush buffer to all registered sinks. Returns total persisted.

        Raises the first sink error after every sink was attempted; the
        failed sinks keep their entries pending for the next flush.
        """
        with self._lock:
            batch, self._buffer = self._buffer, []
            work: list[tuple[int, AuditSink, list[AuditEntry]]] = []
            dropped = 0
            for index, sink in enumerate(self._sinks):
                pending = self._pending[index]
                pending.extend(batch)
                dropped += self._trim(pending)
                self._pending[index] = []
                work.append((index, sink, pending))
        self._warn_dropped(dropped)

        total = 0
        first_error: Exception | None = None
        for index, sink, entries in work:
            if not entries:
                continue
            try:
                total += await sink.write_batch(entries)
            except Exception as exc:
                logger.exception("Audit sink %s failed, keeping %d entries pending", type(sink).__name__, len(entries))
                with self._lock:
                    self._pending[index] = entries + self._pending[index]
                    dropped = self._trim(self._pending[index])
                self._warn_dropped(dropped)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return total

    def get_retention_days(self, compliance_tags: Sequence[str]) -> int:
        if not compliance_tags:
            return 365
        days = [self._retention[t].retention_days for t in compliance_tags if t in self._retention]
        return max(days) if days else 365

    @property
    def buffer_count(self) -> int:
        """Distinct entries not yet delivered to every sink."""
        with self._lock:
            pending_ids = {e.audit_id for pending in self._pending for e in pending}
            return len(self._buffer) + len(pending_ids)

    @property
    def dropped_count(self) -> int:
        """Entries discarded because the buffer or a pending list overflowed."""
        with self._lock:
            return self._dropped

    def _trim(self, entries: list[AuditEntry]) -> int:
        """Drop the oldest entries beyond ``buffer_size``. Caller holds the lock."""
        excess = len(entries) - self._buffer_size
        if excess <= 0:
            return 0
        del entries[:excess]
        self._dropped += excess
        return excess

    def _warn_dropped(self, dropped: int) -> None:
        if dropped:
            logger.warning(
                "Audit buffer over buffer_size=%d: dropped %d oldest entries (total dropped=%d)",
                self._buffer_size,
                dropped,
                self._dropped,
            )

    def _emit(self, entry: AuditEntry) -> None:
        self._log.info("audit_event", extra={"audit": entry.model_dump(mode="json")})
        with self._lock:
            self._buffer.append(entry)
            dropped = self._trim(self._buffer)
        self._warn_dropped(dropped)
