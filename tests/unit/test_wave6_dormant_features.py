"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, wave6]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for Wave 6: Dormant Feature Activation.
Tests admin subactions: trigger_kge, kge_status, erase_subject,
gds_status, gds_trigger, gds_health, feature_status.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.handlers import handle_admin, init_dependencies

pytestmark = pytest.mark.unit

# ── Fixtures ────────────────────────────────────────────────


def _make_graph_driver() -> MagicMock:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    return driver


def _make_domain_loader(spec: MagicMock | None = None) -> MagicMock:
    loader = MagicMock()
    if spec is None:
        spec = _make_basic_spec()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = ["test_domain"]
    return loader


def _make_basic_spec() -> MagicMock:
    spec = MagicMock()
    spec.domain.id = "test_domain"
    spec.kge = None
    spec.gdsjobs = []
    spec.compliance = None
    spec.calibration = None
    spec.sync = None
    return spec


class _ErasureRecorder:
    """Fake driver + pool for erase_subject that record what reaches each store (RU-066)."""

    def __init__(self) -> None:
        self.cypher: list[str] = []
        self.sql: list[tuple] = []
        self.erasure_entries_before_delete = -1
        self.erasure_entries_before_sql = -1
        self.persisted_before_delete = -1
        self.persisted_before_sql = -1
        self.transaction_depth = 0
        self.in_transaction: list[bool] = []
        # rows the ComplianceEngine audit pool received (packet_audit_log inserts)
        self.persisted_rows: list[tuple] = []

    def _erasure_entries(self) -> int:
        """PII_ERASURE CRITICAL entries recorded so far: buffered or persisted."""
        from engine.compliance.audit import AuditAction, AuditSeverity
        from engine.state import get_state

        engine = get_state().compliance_engines.get("test_domain")
        buffered = 0
        if engine is not None:
            buffered = sum(
                1
                for e in engine._audit._buffer
                if e.action == AuditAction.PII_ERASURE and e.severity == AuditSeverity.CRITICAL
            )
        return buffered + self._persisted_erasure_entries()

    def _persisted_erasure_entries(self) -> int:
        # flush_to_store row layout: (audit_id, timestamp, action, severity, ...)
        return sum(1 for row in self.persisted_rows if row[2] == "pii_erasure" and row[3] == "critical")

    def audit_pool(self, *, fail: bool = False):
        """Fake POSTGRES_DSN audit pool; ``fail`` makes every flush raise."""
        recorder = self

        class _AuditConn:
            async def executemany(self, sql, rows):
                if fail:
                    msg = "audit sink unavailable"
                    raise ConnectionError(msg)
                recorder.persisted_rows.extend(rows)

        class _AuditPool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return _AuditConn()

                    async def __aexit__(self, *exc):
                        return None

                return _Ctx()

        return _AuditPool()

    async def run(
        self,
        monkeypatch,
        *,
        subject_field,
        subject_id,
        dry_run=False,
        packet_store=True,
        audit_pool="default",
    ):
        from engine.config.schema import ComplianceSpec
        from engine.config.settings import settings
        from engine.state import get_state

        recorder = self

        async def execute_query(cypher, parameters=None, database=None):
            recorder.cypher.append(cypher)
            if "DETACH DELETE" in cypher:
                recorder.erasure_entries_before_delete = recorder._erasure_entries()
                recorder.persisted_before_delete = recorder._persisted_erasure_entries()
                return [{"deleted": 3}]
            return [{"cnt": 2}]

        class _Conn:
            async def fetchval(self, sql, *args):
                recorder.sql.append((sql, args))
                recorder.in_transaction.append(recorder.transaction_depth > 0)
                if "gdpr_erase_subject" in sql:
                    recorder.erasure_entries_before_sql = recorder._erasure_entries()
                    recorder.persisted_before_sql = recorder._persisted_erasure_entries()
                return 7

            def transaction(self):
                class _Tx:
                    async def __aenter__(self):
                        recorder.transaction_depth += 1

                    async def __aexit__(self, *exc):
                        recorder.transaction_depth -= 1

                return _Tx()

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return _Conn()

                    async def __aexit__(self, *exc):
                        return None

                return _Ctx()

        class _Store:
            async def get_pool(self):
                return _Pool()

        monkeypatch.setattr(settings, "gdpr_erasure_enabled", True)
        monkeypatch.setattr(settings, "gdpr_dry_run", dry_run)
        monkeypatch.setattr(settings, "packet_store_enabled", packet_store)
        monkeypatch.setattr("engine.packet.packet_store.get_packet_store", _Store)
        driver = _make_graph_driver()
        driver.execute_query = AsyncMock(side_effect=execute_query)
        spec = _make_basic_spec()
        spec.compliance = ComplianceSpec(gdpr_subject_id_field=subject_field)
        # The pre-delete audit flush is strict (RU-066 follow-up), so a live
        # erasure needs the POSTGRES_DSN audit pool; None models it unset.
        pool = self.audit_pool() if audit_pool == "default" else audit_pool
        init_dependencies(driver, _make_domain_loader(spec), db_pool=pool)
        get_state().compliance_engines.pop("test_domain", None)
        try:
            # RU-003: an admin domain_id must equal the packet tenant.
            return await handle_admin(
                "test_domain",
                {"subaction": "erase_subject", "data_subject_id": subject_id, "domain_id": "test_domain"},
            )
        finally:
            self.final_entries = self._erasure_entries()
            get_state().compliance_engines.pop("test_domain", None)
            get_state()._db_pool = None


def _make_kge_spec() -> MagicMock:
    spec = _make_basic_spec()
    kge = MagicMock()
    kge.model = "CompoundE3D"
    kge.embeddingdim = 256
    kge.trainingrelations = ["SUPPLIES", "CONSUMES"]
    kge.vectorindex = MagicMock()
    kge.vectorindex.name = "test_kge_index"
    spec.kge = kge
    return spec


def _make_gds_spec() -> MagicMock:
    from engine.config.schema import GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec

    spec = _make_basic_spec()
    job = MagicMock(spec=GDSJobSpec)
    job.name = "louvain_community"
    job.algorithm = "louvain"
    job.schedule = MagicMock(spec=GDSJobScheduleSpec)
    job.schedule.type = "cron"
    job.schedule.cron = "0 2 * * *"
    job.projection = MagicMock(spec=GDSProjectionSpec)
    job.projection.nodelabels = ["Facility"]
    job.projection.edgetypes = ["SUPPLIES"]
    spec.gdsjobs = [job]
    return spec


# ═══════════════════════════════════════════════════════════════
# W6-04: Feature Status
# ═══════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestFeatureStatus:
    """Test the feature_status admin subaction."""

    @pytest.mark.asyncio
    async def test_feature_status_returns_all_flags(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        result = await handle_admin("test_tenant", {"subaction": "feature_status"})

        assert result["status"] == "ok"
        gates = result["feature_gates"]
        # Verify all expected flags are present
        assert "kge_enabled" in gates
        assert "gds_enabled" in gates
        assert "feedback_enabled" in gates
        assert "score_normalize" in gates
        assert "gdpr_erasure_enabled" in gates
        assert "gdpr_dry_run" in gates
        assert "gds_max_staleness_hours" in gates
        assert "domain_strict_validation" in gates
        assert "score_clamp_enabled" in gates
        assert "confidence_check_enabled" in gates
        assert "pareto_enabled" in gates

    @pytest.mark.asyncio
    async def test_feature_status_defaults(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        result = await handle_admin("test_tenant", {"subaction": "feature_status"})
        gates = result["feature_gates"]

        # Default OFF
        assert gates["kge_enabled"] is False
        assert gates["feedback_enabled"] is False
        assert gates["score_normalize"] is False
        assert gates["gdpr_erasure_enabled"] is False
        # Default ON
        assert gates["gdpr_dry_run"] is True
        assert gates["gds_enabled"] is True


# ═══════════════════════════════════════════════════════════════
# W6-01: KGE Activation Pathway
# ═══════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestTriggerKGE:
    """Test the trigger_kge admin subaction."""

    @pytest.mark.asyncio
    async def test_trigger_kge_disabled(self):
        """When KGE_ENABLED=False, trigger_kge raises FeatureNotEnabled."""
        from chassis.errors import FeatureNotEnabled

        driver = _make_graph_driver()
        loader = _make_domain_loader(_make_kge_spec())
        init_dependencies(driver, loader)

        with pytest.raises(FeatureNotEnabled, match="KGE"):
            await handle_admin(
                "test_domain",
                {"subaction": "trigger_kge", "domain_id": "test_domain"},
            )

    @pytest.mark.asyncio
    async def test_trigger_kge_no_spec(self):
        """When domain spec has no kge section, raise ValidationError."""
        from engine.handlers import ValidationError

        driver = _make_graph_driver()
        spec = _make_basic_spec()
        spec.kge = None
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            with pytest.raises(ValidationError, match="no 'kge' section"):
                await handle_admin(
                    "test_domain",
                    {"subaction": "trigger_kge", "domain_id": "test_domain"},
                )

    @pytest.mark.asyncio
    async def test_trigger_kge_dim_mismatch(self):
        """When spec dim differs from settings dim, raise ValidationError."""
        from engine.handlers import ValidationError

        driver = _make_graph_driver()
        spec = _make_kge_spec()
        spec.kge.embeddingdim = 128  # Mismatch with default 256
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            with pytest.raises(ValidationError, match="differs from"):
                await handle_admin(
                    "test_domain",
                    {"subaction": "trigger_kge", "domain_id": "test_domain"},
                )

    @pytest.mark.asyncio
    async def test_trigger_kge_success(self):
        """When all conditions met, returns activation status."""
        driver = _make_graph_driver()
        spec = _make_kge_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            mock_settings.kge_confidence_threshold = 0.3
            result = await handle_admin(
                "test_domain",
                {"subaction": "trigger_kge", "domain_id": "test_domain"},
            )

        assert result["status"] in ("kge_activated", "kge_activated_with_warnings")
        assert result["embedding_dim"] == 256
        assert result["model"] == "CompoundE3D"
        assert result["training_relations"] == ["SUPPLIES", "CONSUMES"]
        assert "smoke_test" in result

    @pytest.mark.asyncio
    async def test_trigger_kge_smoke_test_failure(self):
        """When vector index check fails, returns activated_with_warnings."""
        driver = _make_graph_driver()
        driver.execute_query = AsyncMock(side_effect=Exception("index not found"))
        spec = _make_kge_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            mock_settings.kge_confidence_threshold = 0.3
            result = await handle_admin(
                "test_domain",
                {"subaction": "trigger_kge", "domain_id": "test_domain"},
            )

        assert result["status"] == "kge_activated_with_warnings"
        assert result["smoke_test"]["ok"] is False


@pytest.mark.unit
class TestKGEStatus:
    """Test the kge_status admin subaction."""

    @pytest.mark.asyncio
    async def test_kge_status_without_domain(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        result = await handle_admin("test_tenant", {"subaction": "kge_status"})

        assert result["status"] == "ok"
        assert result["kge_enabled"] is False
        assert result["domain_config"] == {}

    @pytest.mark.asyncio
    async def test_kge_status_with_domain(self):
        driver = _make_graph_driver()
        spec = _make_kge_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        result = await handle_admin(
            "test_domain",
            {"subaction": "kge_status", "domain_id": "test_domain"},
        )

        assert result["status"] == "ok"
        assert result["domain_config"]["model"] == "CompoundE3D"
        assert result["domain_config"]["embeddingdim"] == 256


# ═══════════════════════════════════════════════════════════════
# W6-02: GDPR Erasure Endpoint
# ═══════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEraseSubject:
    """Test the erase_subject admin subaction."""

    @pytest.mark.asyncio
    async def test_erase_subject_disabled(self):
        """When GDPR_ERASURE_ENABLED=False, raises FeatureNotEnabled."""
        from chassis.errors import FeatureNotEnabled

        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with pytest.raises(FeatureNotEnabled, match="GDPR Erasure"):
            await handle_admin(
                "test_tenant",
                {"subaction": "erase_subject", "data_subject_id": "user-123"},
            )

    @pytest.mark.asyncio
    async def test_erase_subject_dry_run(self):
        """When GDPR_DRY_RUN=True, returns scope without executing."""
        driver = _make_graph_driver()
        driver.execute_query = AsyncMock(return_value=[{"cnt": 5}])
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.gdpr_erasure_enabled = True
            mock_settings.gdpr_dry_run = True
            # A MagicMock attribute is truthy; name the PacketStore state explicitly.
            mock_settings.packet_store_enabled = False
            result = await handle_admin(
                "test_tenant",
                {"subaction": "erase_subject", "data_subject_id": "user-123"},
            )

        assert result["status"] == "dry_run"
        assert result["dry_run"] is True
        assert result["data_subject_id"] == "user-123"
        assert result["would_affect"]["graph_nodes"] == 5
        # PacketStore disabled: packets are not erasable, so no count (not 0).
        assert result["would_affect"]["packets"] is None

    @pytest.mark.asyncio
    async def test_erase_subject_real_execution(self, monkeypatch):
        """When GDPR_DRY_RUN=False, performs actual erasure through the real PIIHandler.

        RU-066: this test used to replace PIIHandler and AuditLogger with mocks, which
        hid the hardcoded subject property, the 1-argument SQL and the discarded
        AuditLogger. It now drives the real handler with a fake driver and pool.
        """
        recorder = _ErasureRecorder()
        result = await recorder.run(monkeypatch, subject_field="data_subject_id", subject_id="user-123")

        assert result["status"] == "erased"
        assert result["dry_run"] is False
        assert result["summary"]["graph_nodes_deleted"] == 3
        assert recorder.cypher == ["MATCH (n {data_subject_id: $dsid}) DETACH DELETE n RETURN count(n) AS deleted"]
        assert recorder.erasure_entries_before_delete == 1
        assert recorder.persisted_before_delete == 1

    @pytest.mark.asyncio
    async def test_erase_subject_uses_configured_subject_field_and_tenant_scoped_sql(self, monkeypatch):
        """RU-066 follow-up (PR #352 review): the packet erasure is scoped to the
        requesting tenant. Before, ``gdpr_erase_subject($1, $2)`` filtered on the
        subject id only (another tenant's packets for a reused id were deleted
        under an owner DSN), and no app.current_tenant was set (under the RLS
        l9_product DSN every row was hidden and the call reported 0)."""
        recorder = _ErasureRecorder()
        result = await recorder.run(monkeypatch, subject_field="customer_id", subject_id="cust-42")

        assert recorder.cypher == ["MATCH (n {customer_id: $dsid}) DETACH DELETE n RETURN count(n) AS deleted"]
        assert recorder.sql == [
            ("SELECT set_config('app.current_tenant', $1, true)", ("test_domain",)),
            ("SELECT gdpr_erase_subject($1, $2, $3)", ("cust-42", "test_domain", "test_domain")),
        ]
        # transaction-local GUC: both statements run inside one transaction
        assert recorder.in_transaction == [True, True]
        assert result["summary"]["packets_deleted"] == 7
        assert isinstance(result["summary"]["packets_deleted"], int)

    @pytest.mark.asyncio
    async def test_erase_subject_audit_entry_precedes_any_delete(self, monkeypatch):
        recorder = _ErasureRecorder()
        await recorder.run(monkeypatch, subject_field="customer_id", subject_id="cust-42")
        # The PII_ERASURE CRITICAL entry was recorded, and persisted to the audit
        # sink (not merely buffered), before both the pool and the driver
        # recorded their delete.
        assert recorder.erasure_entries_before_sql == 1
        assert recorder.erasure_entries_before_delete == 1
        assert recorder.persisted_before_sql == 1
        assert recorder.persisted_before_delete == 1

    @pytest.mark.asyncio
    async def test_erase_subject_failed_audit_flush_blocks_every_delete(self, monkeypatch):
        """RU-066 follow-up: a failed pre-delete audit flush blocks the erasure.

        Before, ``flush_audit_for_request`` honoured ``compliance_audit_soft_sink``
        (default True): the flush failure was logged and the packet and graph
        deletes ran with no durable audit entry."""
        from engine.config.settings import settings
        from engine.handlers import ExecutionError

        monkeypatch.setattr(settings, "compliance_audit_soft_sink", True)
        recorder = _ErasureRecorder()
        with pytest.raises(ExecutionError, match="GDPR erasure failed") as exc_info:
            await recorder.run(
                monkeypatch,
                subject_field="customer_id",
                subject_id="cust-42",
                audit_pool=recorder.audit_pool(fail=True),
            )
        assert "audit sink unavailable" in str(exc_info.value.detail)
        assert recorder.sql == []
        assert recorder.cypher == []
        # the entry is kept (re-buffered) for the periodic flush, not lost
        assert recorder.final_entries == 1

    @pytest.mark.asyncio
    async def test_erase_subject_without_audit_sink_blocks_every_delete(self, monkeypatch):
        """No POSTGRES_DSN audit pool: the erasure entry could not be made
        durable, so nothing is deleted (before, flush returned 0 and the
        deletes ran)."""
        from engine.handlers import ExecutionError

        recorder = _ErasureRecorder()
        with pytest.raises(ExecutionError, match="GDPR erasure failed") as exc_info:
            await recorder.run(monkeypatch, subject_field="customer_id", subject_id="cust-42", audit_pool=None)
        assert "durable audit sink" in str(exc_info.value.detail)
        assert recorder.sql == []
        assert recorder.cypher == []

    @pytest.mark.asyncio
    async def test_erase_subject_packet_store_disabled_reports_not_erasable(self, monkeypatch):
        recorder = _ErasureRecorder()
        result = await recorder.run(monkeypatch, subject_field="customer_id", subject_id="c", packet_store=False)
        assert recorder.sql == []
        assert result["summary"]["packets_deleted"] is None
        assert result["summary"]["packets_erasable"] is False

    @pytest.mark.asyncio
    async def test_erase_subject_dry_run_uses_configured_subject_field(self, monkeypatch):
        recorder = _ErasureRecorder()
        result = await recorder.run(monkeypatch, subject_field="customer_id", subject_id="cust-42", dry_run=True)
        assert result["status"] == "dry_run"
        assert recorder.cypher == ["MATCH (n {customer_id: $dsid}) RETURN count(n) AS cnt"]
        # RU-066 follow-up: the dry run also counts the requesting tenant's
        # packets, read-only, inside one transaction with the tenant GUC set.
        assert recorder.sql == [
            ("SELECT set_config('app.current_tenant', $1, true)", ("test_domain",)),
            (
                "SELECT count(*) FROM packet_store WHERE data_subject_id = $1 AND actor_tenant = $2",
                ("cust-42", "test_domain"),
            ),
        ]
        assert recorder.in_transaction == [True, True]
        assert result["would_affect"] == {"graph_nodes": 2, "packets": 7}
        assert recorder.persisted_rows == []

    @pytest.mark.asyncio
    async def test_erase_subject_dry_run_packet_store_disabled_reports_none(self, monkeypatch):
        recorder = _ErasureRecorder()
        result = await recorder.run(
            monkeypatch, subject_field="customer_id", subject_id="cust-42", dry_run=True, packet_store=False
        )
        assert recorder.sql == []
        assert result["would_affect"] == {"graph_nodes": 2, "packets": None}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dry_run", [True, False])
    async def test_erase_subject_invalid_subject_field_raises_before_any_query(self, monkeypatch, dry_run):
        recorder = _ErasureRecorder()
        with pytest.raises(ValueError, match="Invalid label or type"):
            await recorder.run(monkeypatch, subject_field="a.b", subject_id="x", dry_run=dry_run)
        assert recorder.cypher == []
        assert recorder.sql == []

    @pytest.mark.asyncio
    async def test_erase_subject_missing_id(self):
        """When data_subject_id is missing, raises ValidationError."""
        from engine.handlers import ValidationError

        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.gdpr_erasure_enabled = True
            with pytest.raises(ValidationError, match="data_subject_id"):
                await handle_admin(
                    "test_tenant",
                    {"subaction": "erase_subject"},
                )

    @pytest.mark.asyncio
    async def test_erase_subject_idempotent_dry_run(self):
        """Dry-run with zero nodes returns zero count."""
        driver = _make_graph_driver()
        driver.execute_query = AsyncMock(return_value=[{"cnt": 0}])
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.gdpr_erasure_enabled = True
            mock_settings.gdpr_dry_run = True
            mock_settings.packet_store_enabled = False
            result = await handle_admin(
                "test_tenant",
                {"subaction": "erase_subject", "data_subject_id": "already-erased"},
            )

        assert result["status"] == "dry_run"
        assert result["would_affect"]["graph_nodes"] == 0


# ═══════════════════════════════════════════════════════════════
# W6-03: GDS Job History Exposure
# ═══════════════════════════════════════════════════════════════


def _make_scheduler_with_history(history: list[dict] | None = None) -> MagicMock:
    scheduler = MagicMock()
    if history is None:
        history = []
    scheduler.get_job_history = AsyncMock(return_value=history)
    scheduler.trigger_job = AsyncMock(return_value={"status": "success"})
    scheduler.register_jobs = MagicMock()
    scheduler.start = MagicMock()
    return scheduler


@pytest.mark.unit
class TestGDSStatus:
    """Test the gds_status admin subaction."""

    @pytest.mark.asyncio
    async def test_gds_status_empty_history(self):
        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.handlers._get_or_create_scheduler") as mock_get:
            mock_get.return_value = _make_scheduler_with_history([])
            result = await handle_admin(
                "test_domain",
                {"subaction": "gds_status", "domain_id": "test_domain"},
            )

        assert result["status"] == "ok"
        assert result["history_count"] == 0
        assert result["last_runs"] == {}

    @pytest.mark.asyncio
    async def test_gds_status_with_history(self):
        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        history = [
            {
                "job": "louvain_community",
                "algorithm": "louvain",
                "timestamp": "2026-03-23T02:00:00",
                "status": "success",
            },
            {
                "job": "louvain_community",
                "algorithm": "louvain",
                "timestamp": "2026-03-22T02:00:00",
                "status": "success",
            },
        ]

        with patch("engine.handlers._get_or_create_scheduler") as mock_get:
            mock_get.return_value = _make_scheduler_with_history(history)
            result = await handle_admin(
                "test_domain",
                {"subaction": "gds_status", "domain_id": "test_domain"},
            )

        assert result["status"] == "ok"
        assert result["history_count"] == 2
        assert "louvain_community" in result["last_runs"]
        assert result["last_runs"]["louvain_community"]["status"] == "success"


@pytest.mark.unit
class TestGDSTrigger:
    """Test the gds_trigger admin subaction."""

    @pytest.mark.asyncio
    async def test_gds_trigger_valid_job(self):
        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.handlers._get_or_create_scheduler") as mock_get:
            mock_get.return_value = _make_scheduler_with_history()
            result = await handle_admin(
                "test_domain",
                {"subaction": "gds_trigger", "domain_id": "test_domain", "job_name": "louvain_community"},
            )

        assert result["status"] == "triggered"
        assert result["job"] == "louvain_community"

    @pytest.mark.asyncio
    async def test_gds_trigger_invalid_job(self):
        """When job_name is not in domain spec, raises ValidationError."""
        from engine.handlers import ValidationError

        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with pytest.raises(ValidationError, match="not found in domain spec"):
            await handle_admin(
                "test_domain",
                {"subaction": "gds_trigger", "domain_id": "test_domain", "job_name": "nonexistent_job"},
            )


@pytest.mark.unit
class TestGDSHealth:
    """Test the gds_health admin subaction."""

    @pytest.mark.asyncio
    async def test_gds_health_all_healthy(self):
        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        recent_ts = datetime.now(tz=UTC).isoformat()
        history = [
            {
                "job": "louvain_community",
                "algorithm": "louvain",
                "timestamp": recent_ts,
                "status": "success",
            },
        ]

        with patch("engine.handlers._get_or_create_scheduler") as mock_get:
            mock_get.return_value = _make_scheduler_with_history(history)
            result = await handle_admin(
                "test_domain",
                {"subaction": "gds_health", "domain_id": "test_domain"},
            )

        assert result["status"] == "healthy"
        assert result["algorithms"]["louvain_community"]["stale"] is False

    @pytest.mark.asyncio
    async def test_gds_health_stale_job(self):
        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        old_ts = (datetime.now(tz=UTC) - timedelta(hours=30)).isoformat()
        history = [
            {
                "job": "louvain_community",
                "algorithm": "louvain",
                "timestamp": old_ts,
                "status": "success",
            },
        ]

        with patch("engine.handlers._get_or_create_scheduler") as mock_get:
            mock_get.return_value = _make_scheduler_with_history(history)
            result = await handle_admin(
                "test_domain",
                {"subaction": "gds_health", "domain_id": "test_domain"},
            )

        assert result["status"] == "degraded"
        assert result["algorithms"]["louvain_community"]["stale"] is True

    @pytest.mark.asyncio
    async def test_gds_health_never_run(self):
        """Job defined in spec but never executed should be marked stale."""
        driver = _make_graph_driver()
        spec = _make_gds_spec()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        with patch("engine.handlers._get_or_create_scheduler") as mock_get:
            mock_get.return_value = _make_scheduler_with_history([])
            result = await handle_admin(
                "test_domain",
                {"subaction": "gds_health", "domain_id": "test_domain"},
            )

        assert result["status"] == "degraded"
        assert "louvain_community" in result["algorithms"]
        assert result["algorithms"]["louvain_community"]["status"] == "never_run"
        assert result["algorithms"]["louvain_community"]["stale"] is True


# ═══════════════════════════════════════════════════════════════
# W6-04: FeatureNotEnabled Error
# ═══════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestFeatureNotEnabledError:
    """Test the FeatureNotEnabled exception class."""

    def test_basic_construction(self):
        from chassis.errors import FeatureNotEnabled

        err = FeatureNotEnabled("KGE", flag="KGE_ENABLED")
        assert "KGE" in str(err)
        assert "KGE_ENABLED" in str(err)
        assert err.status_code == 422
        assert err.feature == "KGE"
        assert err.flag == "KGE_ENABLED"

    def test_custom_message(self):
        from chassis.errors import FeatureNotEnabled

        err = FeatureNotEnabled("test", flag="TEST_FLAG", message="Custom message")
        assert str(err) == "Custom message"

    def test_to_dict(self):
        from chassis.errors import FeatureNotEnabled

        err = FeatureNotEnabled("KGE", flag="KGE_ENABLED", action="admin", tenant="t1")
        d = err.to_dict()
        assert d["error"] == "FeatureNotEnabled"
        assert d["action"] == "admin"
        assert d["tenant"] == "t1"

    def test_llm_client_raises_feature_not_enabled(self, monkeypatch):
        """ValidatedLLMClient._call() should raise FeatureNotEnabled."""
        pytest.importorskip("structlog")
        from chassis.errors import FeatureNotEnabled
        from engine.security import P2_9_llm_schemas as llm_mod
        from engine.security.P2_9_llm_schemas import ValidatedLLMClient, _LLMBackend

        # Hermetic: hide any host OPENAI_API_KEY and reset the cached backend
        # so _call() always hits the not-configured path.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(llm_mod, "_llm_backend", _LLMBackend())

        client = ValidatedLLMClient()
        with pytest.raises(FeatureNotEnabled, match="LLM"):
            client._call("system", "user")


# ═══════════════════════════════════════════════════════════════
# W6-02: Schema Extension
# ═══════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGDPRSchemaExtension:
    """Test the gdpr_subject_id_field on ComplianceSpec."""

    def test_default_gdpr_subject_id_field(self):
        from engine.config.schema import ComplianceSpec

        compliance = ComplianceSpec()
        assert compliance.gdpr_subject_id_field == "data_subject_id"

    def test_custom_gdpr_subject_id_field(self):
        from engine.config.schema import ComplianceSpec

        compliance = ComplianceSpec(gdpr_subject_id_field="customer_id")
        assert compliance.gdpr_subject_id_field == "customer_id"


# ═══════════════════════════════════════════════════════════════
# W6-03: Boot GDS Health Probe Registration
# ═══════════════════════════════════════════════════════════════


def _place_scheduler(monkeypatch, lifecycle, scheduler, *, shared_registry: bool) -> None:
    """Put a scheduler where the probe reads it (RU-007 / gds_shared_scheduler_registry)."""
    from engine.config.settings import settings
    from engine.state import get_state

    monkeypatch.setattr(settings, "gds_shared_scheduler_registry", shared_registry)
    monkeypatch.setattr(get_state(), "_gds_schedulers", {})
    if shared_registry:
        get_state().gds_schedulers["test_domain"] = scheduler
    else:
        lifecycle._schedulers = [scheduler]


@pytest.mark.unit
class TestGDSHealthProbeRegistration:
    """Test that boot.py registers the GDS health probe."""

    def test_health_probe_method_exists(self):
        from engine.boot import GraphLifecycle

        lifecycle = GraphLifecycle()
        assert hasattr(lifecycle, "_register_gds_health_probe")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shared_registry", [True, False], ids=["registry", "boot_list"])
    async def test_health_probe_returns_ok(self, monkeypatch, shared_registry):
        from engine.boot import GraphLifecycle

        lifecycle = GraphLifecycle()
        # Simulate schedulers with recent history
        recent_ts = datetime.now(tz=UTC).isoformat()
        scheduler = MagicMock()
        scheduler.get_job_history = AsyncMock(
            return_value=[
                {"algorithm": "louvain", "timestamp": recent_ts, "status": "success"},
            ]
        )
        _place_scheduler(monkeypatch, lifecycle, scheduler, shared_registry=shared_registry)
        lifecycle._register_gds_health_probe()

        result = await lifecycle._gds_health_check()
        assert result["gds"] == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shared_registry", [True, False], ids=["registry", "boot_list"])
    async def test_health_probe_returns_degraded(self, monkeypatch, shared_registry):
        from engine.boot import GraphLifecycle

        lifecycle = GraphLifecycle()
        old_ts = (datetime.now(tz=UTC) - timedelta(hours=30)).isoformat()
        scheduler = MagicMock()
        scheduler.get_job_history = AsyncMock(
            return_value=[
                {"algorithm": "louvain", "timestamp": old_ts, "status": "success"},
            ]
        )
        _place_scheduler(monkeypatch, lifecycle, scheduler, shared_registry=shared_registry)
        lifecycle._register_gds_health_probe()

        result = await lifecycle._gds_health_check()
        assert "degraded" in result["gds"]


class _FakeErasePool:
    """Pool whose connection records every statement and transaction nesting."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, bool]] = []
        self._depth = 0

    def acquire(self):
        pool = self

        class _Conn:
            async def fetchval(self, sql, *args):
                pool.calls.append((sql, args, pool._depth > 0))
                return 1

            def transaction(self):
                class _Tx:
                    async def __aenter__(self):
                        pool._depth += 1

                    async def __aexit__(self, *exc):
                        pool._depth -= 1

                return _Tx()

        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


class TestPiiEraseSubjectTenantScope:
    """RU-066 follow-up: PIIHandler.erase_subject scopes packet erasure to one tenant."""

    @pytest.mark.asyncio
    async def test_packet_erasure_sets_tenant_and_passes_it(self) -> None:
        from engine.compliance.pii import PIIHandler

        pool = _FakeErasePool()
        summary = await PIIHandler().erase_subject("subj-1", db_pool=pool, tenant="tenant-a", performed_by="tenant-a")

        assert pool.calls == [
            ("SELECT set_config('app.current_tenant', $1, true)", ("tenant-a",), True),
            ("SELECT gdpr_erase_subject($1, $2, $3)", ("subj-1", "tenant-a", "tenant-a"), True),
        ]
        assert summary["packets_deleted"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant", [None, ""])
    async def test_packet_erasure_without_tenant_raises_before_any_sql(self, tenant) -> None:
        from engine.compliance.pii import PIIHandler

        pool = _FakeErasePool()
        with pytest.raises(ValueError, match="tenant"):
            await PIIHandler().erase_subject("subj-1", db_pool=pool, tenant=tenant)
        assert pool.calls == []

    @pytest.mark.asyncio
    async def test_rls_session_flag_off_skips_set_config_but_still_scopes(self, monkeypatch) -> None:
        from engine.compliance.pii import PIIHandler
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "packet_store_rls_tenant_session_enabled", False)
        pool = _FakeErasePool()
        await PIIHandler().erase_subject("subj-1", db_pool=pool, tenant="tenant-a", performed_by="tenant-a")

        assert [c[0] for c in pool.calls] == ["SELECT gdpr_erase_subject($1, $2, $3)"]
        assert pool.calls[0][1] == ("subj-1", "tenant-a", "tenant-a")
