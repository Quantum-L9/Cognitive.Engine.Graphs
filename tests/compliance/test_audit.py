"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, compliance, audit]
owner: engine-team
status: active
--- /L9_META ---

Audit logging compliance tests.
Target Coverage: 90%+
"""

import pytest

from engine.compliance.audit import DEFAULT_RETENTION, AuditAction, AuditLogger, AuditSeverity
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT


@pytest.mark.compliance
@pytest.mark.unit
class TestAuditLogging:
    """Test audit log generation."""

    def test_log_access(self) -> None:
        """Access events are logged with correct structure."""
        logger = AuditLogger()

        entry = logger.log_access(
            actor="test_user",
            tenant="plasticos",
            resource="Facility:42",
            resource_type="Facility",
            trace_id="trace-123",
        )

        assert entry.action == AuditAction.ACCESS
        assert entry.actor == "test_user"
        assert entry.tenant == "plasticos"
        assert entry.resource == "Facility:42"
        assert entry.outcome == "success"

    def test_log_mutation(self) -> None:
        """Mutation events are logged with payload hash."""
        logger = AuditLogger()

        entry = logger.log_mutation(
            actor="sync_service",
            tenant="plasticos",
            resource="MaterialProfile:7",
            detail="Batch sync 150 entities",
            payload_hash="abc123hash",
            compliance_tags=["GDPR", "SOC2"],
        )

        assert entry.action == AuditAction.MUTATION
        assert entry.detail == "Batch sync 150 entities"
        assert entry.payload_hash == "abc123hash"
        assert "GDPR" in entry.compliance_tags

    def test_log_query(self) -> None:
        """Query events are logged."""
        logger = AuditLogger()

        entry = logger.log_query(
            actor="api",
            tenant="mortgage",
            detail="match_strict 14 gates",
            trace_id="trace-456",
        )

        assert entry.action == AuditAction.QUERY
        assert entry.severity == AuditSeverity.INFO

    def test_log_pii_erasure(self) -> None:
        """PII erasure events are logged with CRITICAL severity."""
        logger = AuditLogger()

        entry = logger.log_pii_erasure(
            actor="gdpr_service",
            tenant="healthcare",
            data_subject_id="user-123",
            detail="Right-to-erasure request processed",
        )

        assert entry.action == AuditAction.PII_ERASURE
        assert entry.severity == AuditSeverity.CRITICAL
        assert entry.data_subject_id == "user-123"
        assert "GDPR" in entry.compliance_tags

    def test_pii_access_warning_severity(self) -> None:
        """Access with PII fields triggers WARNING severity."""
        logger = AuditLogger()

        entry = logger.log_access(
            actor="analyst",
            tenant="healthcare",
            resource="Patient:99",
            pii_fields_accessed=["ssn", "dob"],
        )

        assert entry.severity == AuditSeverity.WARNING
        assert "ssn" in entry.pii_fields_accessed

    def test_retention_policy_lookup(self) -> None:
        """Retention policies return correct days."""
        logger = AuditLogger(retention_policies=DEFAULT_RETENTION)

        # SOC2 = 2555 days (7 years)
        assert logger.get_retention_days(["SOC2"]) == 2555

        # HIPAA = 2190 days (6 years)
        assert logger.get_retention_days(["HIPAA"]) == 2190

        # Multiple tags = longest retention wins
        assert logger.get_retention_days(["GDPR", "SOC2"]) == 2555

        # No tags = default 365
        assert logger.get_retention_days([]) == 365

    @pytest.mark.asyncio
    async def test_buffer_and_flush(self) -> None:
        """Audit entries buffer and flush correctly."""
        logger = AuditLogger(buffer_size=3)

        logger.log_access(actor="u1", tenant="t1", resource="r1")
        logger.log_access(actor="u2", tenant="t1", resource="r2")

        assert logger.buffer_count == 2

        entries = await logger.flush()
        assert len(entries) == 2
        assert logger.buffer_count == 0


# ── RU-065 / CEG-COR-052/053: bounded buffer, soft sink, JSONB metadata ──


class _FakeConn:
    def __init__(self, fail: bool, calls: list) -> None:
        self._fail = fail
        self._calls = calls

    async def executemany(self, sql: str, rows: list) -> None:
        if self._fail:
            msg = "postgres unavailable"
            raise ConnectionError(msg)
        for row in rows:
            assert isinstance(row[15], str), "metadata ($16) must be bound as JSON text for JSONB"
        self._calls.append(rows)


class _FakePool:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list = []

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(pool.fail, pool.calls)

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


@pytest.mark.compliance
@pytest.mark.unit
class TestAuditSoftSink:
    def test_buffer_is_bounded_with_drop_count(self, caplog: pytest.LogCaptureFixture) -> None:
        audit = AuditLogger(buffer_size=100)
        with caplog.at_level("WARNING", logger="engine.compliance.audit"):
            for i in range(10_000):
                audit.log_access(actor="a", tenant="t", resource=f"r{i}")
        assert audit.buffer_count == 100
        assert audit.dropped_count == 9_900
        assert any("dropped" in r.getMessage() for r in caplog.records)

    def test_bounded_buffer_keeps_newest_entries(self) -> None:
        audit = AuditLogger(buffer_size=3)
        for i in range(5):
            audit.log_access(actor="a", tenant="t", resource=f"r{i}")
        assert [e.resource for e in audit.flush_sync()] == ["r2", "r3", "r4"]

    @pytest.mark.asyncio
    async def test_emit_async_is_bounded(self) -> None:
        from engine.compliance.audit import AuditEntry

        audit = AuditLogger(buffer_size=10)
        for _ in range(50):
            await audit.emit_async(AuditEntry(action=AuditAction.ACCESS, actor="a", tenant="t"))
        assert audit.buffer_count == 10
        assert audit.dropped_count == 40

    @pytest.mark.asyncio
    async def test_failing_sink_rebuffer_never_exceeds_bound(self) -> None:
        audit = AuditLogger(buffer_size=5)
        pool = _FakePool(fail=True)
        for _round in range(4):
            for i in range(5):
                audit.log_access(actor="a", tenant="t", resource=f"r{i}")
            with pytest.raises(ConnectionError):
                await audit.flush_to_store(pool)
            assert audit.buffer_count <= 5
        assert audit.dropped_count == 15

    @pytest.mark.asyncio
    async def test_flush_binds_metadata_as_json_text(self) -> None:
        import json

        audit = AuditLogger()
        audit.log_access(actor="a", tenant="t", resource="r", metadata={"k": "v", "n": 1})
        audit.log_query(actor="a", tenant="t", detail="q")
        pool = _FakePool()
        assert await audit.flush_to_store(pool) == 2
        assert json.loads(pool.calls[0][0][15]) == {"k": "v", "n": 1}
        assert audit.buffer_count == 0

    @pytest.mark.asyncio
    async def test_flush_binds_pydantic_json_metadata(self) -> None:
        """Metadata the entry accepts (datetime, UUID, Decimal, NaN) persists as JSON text."""
        import json
        import uuid
        from datetime import UTC, datetime
        from decimal import Decimal

        audit = AuditLogger()
        at = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        audit.log_access(
            actor="a",
            tenant="t",
            resource="r",
            metadata={"at": at, "id": uid, "amount": Decimal("1.5"), "ratio": float("nan")},
        )
        audit.log_query(actor="a", tenant="t", detail="q")
        pool = _FakePool()
        assert await audit.flush_to_store(pool) == 2
        assert json.loads(pool.calls[0][0][15]) == {
            "at": "2026-10-28T12:00:00Z",
            "id": str(uid),
            "amount": "1.5",
            "ratio": None,
        }
        assert audit.buffer_count == 0

    @pytest.mark.asyncio
    async def test_row_build_failure_rebuffers_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure while building the rows keeps the batch buffered, never discards it."""

        from types import SimpleNamespace

        from engine.compliance import audit as audit_module

        def _boom(obj: object) -> str:
            msg = "metadata not serializable"
            raise TypeError(msg)

        audit = AuditLogger()
        audit.log_query(actor="a", tenant="t", detail="q1")
        audit.log_query(actor="a", tenant="t", detail="q2")
        monkeypatch.setattr(audit_module, "json", SimpleNamespace(dumps=_boom))
        pool = _FakePool()
        with pytest.raises(TypeError):
            await audit.flush_to_store(pool)
        assert [e.detail for e in audit.flush_sync()] == ["q1", "q2"]
        assert pool.calls == []

    def test_flag_off_restores_unbounded_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "compliance_audit_soft_sink", False)
        audit = AuditLogger(buffer_size=3)
        for i in range(10):
            audit.log_access(actor="a", tenant="t", resource=f"r{i}")
        assert audit.buffer_count == 10

    @pytest.mark.parametrize("size", [0, -1])
    def test_non_positive_buffer_size_rejected(self, size: int) -> None:
        """A bound below 1 is refused up front, not a ZeroDivisionError on the second emit."""
        with pytest.raises(ValueError, match="buffer_size must be >= 1"):
            AuditLogger(buffer_size=size)

    def test_size_one_buffer_keeps_newest_and_counts_drops(self) -> None:
        audit = AuditLogger(buffer_size=1)
        for i in range(5):
            audit.log_access(actor="a", tenant="t", resource=f"r{i}")
        assert audit.dropped_count == 4
        assert [e.resource for e in audit.flush_sync()] == ["r4"]

    def test_flag_off_accepts_zero_buffer_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "compliance_audit_soft_sink", False)
        audit = AuditLogger(buffer_size=0)
        for i in range(3):
            audit.log_access(actor="a", tenant="t", resource=f"r{i}")
        assert audit.buffer_count == 3

    @pytest.mark.parametrize("size", [0, -5])
    def test_settings_reject_non_positive_compliance_buffer_max(self, size: int) -> None:
        from pydantic import ValidationError

        from engine.config.settings import Settings

        with pytest.raises(ValidationError, match="compliance_buffer_max must be >= 1"):
            Settings(compliance_buffer_max=size)

    def test_settings_accept_zero_buffer_max_with_flag_off(self) -> None:
        from engine.config.settings import Settings

        s = Settings(compliance_buffer_max=0, compliance_audit_soft_sink=False)
        assert s.compliance_buffer_max == 0


@pytest.mark.compliance
@pytest.mark.unit
class TestComplianceEngineSoftSink:
    def _engine(self, pool=None):

        import yaml

        from engine.compliance.engine import ComplianceEngine
        from engine.config.schema import DomainSpec

        raw = yaml.safe_load((DESIGN_PACKS_ROOT / "plasticos/spec.yaml").read_text(encoding="utf-8"))
        return ComplianceEngine(DomainSpec.model_validate(raw), db_pool=pool)

    def test_buffer_bounded_across_5000_requests_without_pool(self) -> None:
        engine = self._engine()
        for _ in range(5000):
            engine.check_match_request(tenant="t", query={"polymer": "HDPE"}, match_direction="d")
        assert engine._audit.buffer_count <= 100

    @pytest.mark.asyncio
    async def test_request_flush_never_raises_on_sink_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = self._engine(_FakePool(fail=True))
        engine.check_match_request(tenant="t", query={}, match_direction="d")
        with caplog.at_level("WARNING"):
            assert await engine.flush_audit_for_request() == 0
        assert any("audit flush failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_boot_style_flush_still_persists(self) -> None:
        pool = _FakePool()
        engine = self._engine(pool)
        engine.check_match_request(tenant="t", query={}, match_direction="d")
        assert await engine.flush_audit() == 1
        assert len(pool.calls) == 1


@pytest.mark.compliance
@pytest.mark.unit
class TestViolationEntryShape:
    """RU-063 / CEG-COR-050: the denied entry carries regimes and the field name, never a value."""

    def test_denied_entry_shape(self) -> None:

        import yaml

        from engine.compliance.engine import ComplianceEngine
        from engine.config.schema import DomainSpec

        raw = yaml.safe_load((DESIGN_PACKS_ROOT / "mortgage-brokerage/spec.yaml").read_text(encoding="utf-8"))
        engine = ComplianceEngine(DomainSpec.model_validate(raw))
        engine._audit.flush_sync()
        with pytest.raises(ValueError):
            engine.check_sync_request(
                tenant="acme", entity_type="Borrower", batch=[{"entity_id": "b1", "Gender": "value-x"}]
            )
        entries = [e for e in engine._audit.flush_sync() if e.outcome == "denied"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.severity in (AuditSeverity.WARNING, AuditSeverity.CRITICAL)
        assert entry.outcome == "denied"
        assert "ECOA" in entry.compliance_tags
        assert "Borrower" in entry.resource
        assert "Gender" in entry.resource
        assert "value-x" not in entry.model_dump_json()
