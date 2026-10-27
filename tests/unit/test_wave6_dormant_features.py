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

    def _erasure_entries(self) -> int:
        from engine.compliance.audit import AuditAction, AuditSeverity
        from engine.state import get_state

        engine = get_state().compliance_engines.get("test_domain")
        if engine is None:
            return 0
        return sum(
            1
            for e in engine._audit._buffer
            if e.action == AuditAction.PII_ERASURE and e.severity == AuditSeverity.CRITICAL
        )

    async def run(self, monkeypatch, *, subject_field, subject_id, dry_run=False, packet_store=True):
        from engine.config.schema import ComplianceSpec
        from engine.config.settings import settings
        from engine.state import get_state

        recorder = self

        async def execute_query(cypher, parameters=None, database=None):
            recorder.cypher.append(cypher)
            if "DETACH DELETE" in cypher:
                recorder.erasure_entries_before_delete = recorder._erasure_entries()
                return [{"deleted": 3}]
            return [{"cnt": 2}]

        class _Conn:
            async def fetchval(self, sql, *args):
                recorder.sql.append((sql, args))
                recorder.erasure_entries_before_sql = recorder._erasure_entries()
                return 7

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
        init_dependencies(driver, _make_domain_loader(spec))
        get_state().compliance_engines.pop("test_domain", None)
        try:
            return await handle_admin(
                "test_tenant",
                {"subaction": "erase_subject", "data_subject_id": subject_id, "domain_id": "test_domain"},
            )
        finally:
            self.final_entries = self._erasure_entries()
            get_state().compliance_engines.pop("test_domain", None)


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
                "test_tenant",
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
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            with pytest.raises(ValidationError, match="no 'kge' section"):
                await handle_admin(
                    "test_tenant",
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
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            with pytest.raises(ValidationError, match="differs from"):
                await handle_admin(
                    "test_tenant",
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
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            mock_settings.kge_confidence_threshold = 0.3
            result = await handle_admin(
                "test_tenant",
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
            mock_settings.kge_enabled = True
            mock_settings.kge_embedding_dim = 256
            mock_settings.kge_confidence_threshold = 0.3
            result = await handle_admin(
                "test_tenant",
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
            "test_tenant",
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
            mock_settings.gdpr_erasure_enabled = True
            mock_settings.gdpr_dry_run = True
            result = await handle_admin(
                "test_tenant",
                {"subaction": "erase_subject", "data_subject_id": "user-123"},
            )

        assert result["status"] == "dry_run"
        assert result["dry_run"] is True
        assert result["data_subject_id"] == "user-123"
        assert result["would_affect"]["graph_nodes"] == 5

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

    @pytest.mark.asyncio
    async def test_erase_subject_uses_configured_subject_field_and_two_arg_sql(self, monkeypatch):
        recorder = _ErasureRecorder()
        result = await recorder.run(monkeypatch, subject_field="customer_id", subject_id="cust-42")

        assert recorder.cypher == ["MATCH (n {customer_id: $dsid}) DETACH DELETE n RETURN count(n) AS deleted"]
        assert recorder.sql == [("SELECT gdpr_erase_subject($1, $2)", ("cust-42", "test_tenant"))]
        assert result["summary"]["packets_deleted"] == 7
        assert isinstance(result["summary"]["packets_deleted"], int)

    @pytest.mark.asyncio
    async def test_erase_subject_audit_entry_precedes_any_delete(self, monkeypatch):
        recorder = _ErasureRecorder()
        await recorder.run(monkeypatch, subject_field="customer_id", subject_id="cust-42")
        # PII_ERASURE CRITICAL entry was in the domain ComplianceEngine buffer when
        # both the pool and the driver recorded their delete.
        assert recorder.erasure_entries_before_sql == 1
        assert recorder.erasure_entries_before_delete == 1

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
        assert recorder.sql == []

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
            mock_settings.gdpr_erasure_enabled = True
            mock_settings.gdpr_dry_run = True
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
                "test_tenant",
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
                "test_tenant",
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
                "test_tenant",
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
                "test_tenant",
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
                "test_tenant",
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
                "test_tenant",
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
                "test_tenant",
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


@pytest.mark.unit
class TestGDSHealthProbeRegistration:
    """Test that boot.py registers the GDS health probe."""

    def test_health_probe_method_exists(self):
        from engine.boot import GraphLifecycle

        lifecycle = GraphLifecycle()
        assert hasattr(lifecycle, "_register_gds_health_probe")

    @pytest.mark.asyncio
    async def test_health_probe_returns_ok(self):
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
        lifecycle._schedulers = [scheduler]
        lifecycle._register_gds_health_probe()

        result = await lifecycle._gds_health_check()
        assert result["gds"] == "ok"

    @pytest.mark.asyncio
    async def test_health_probe_returns_degraded(self):
        from engine.boot import GraphLifecycle

        lifecycle = GraphLifecycle()
        old_ts = (datetime.now(tz=UTC) - timedelta(hours=30)).isoformat()
        scheduler = MagicMock()
        scheduler.get_job_history = AsyncMock(
            return_value=[
                {"algorithm": "louvain", "timestamp": old_ts, "status": "success"},
            ]
        )
        lifecycle._schedulers = [scheduler]
        lifecycle._register_gds_health_probe()

        result = await lifecycle._gds_health_check()
        assert "degraded" in result["gds"]
