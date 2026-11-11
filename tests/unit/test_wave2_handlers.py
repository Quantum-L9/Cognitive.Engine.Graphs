"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, wave2]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for Wave 2 handler integrations.
Tests admin subactions (calibration_run, score_feedback, apply_weight_proposal)
and the outcome sync type.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.handlers import handle_admin, handle_sync, init_dependencies


def _make_graph_driver() -> MagicMock:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    return driver


def _make_domain_loader(spec: MagicMock | None = None, *, domain_feedback: bool = True) -> MagicMock:
    loader = MagicMock()
    if spec is None:
        spec = MagicMock()
        # FEEDBACK-01: the domain opt-in is half of the effective predicate, so
        # the fixture states it explicitly instead of relying on a truthy mock.
        spec.feedbackloop.enabled = domain_feedback
        spec.domain.id = "test_domain"
        spec.calibration = None
        spec.sync = None
        spec.feedbackloop.outcome_node_label = "TransactionOutcome"
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = ["test_domain"]
    return loader


@pytest.mark.unit
class TestCalibrationRunSubaction:
    """Test the calibration_run admin subaction."""

    @pytest.mark.asyncio
    async def test_calibration_run_no_spec(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        result = await handle_admin(
            "test_domain",
            {
                "subaction": "calibration_run",
                "domain_id": "test_domain",
            },
        )

        assert result["status"] == "no_calibration_spec"
        assert result["total_pairs"] == 0

    @pytest.mark.asyncio
    async def test_calibration_run_with_spec(self):
        from engine.config.schema import CalibrationPair, CalibrationSpec

        spec = MagicMock()
        spec.domain.id = "test_domain"
        spec.calibration = CalibrationSpec(
            pairs=[
                CalibrationPair(node_a="A", node_b="B", expected_score_min=0.3, expected_score_max=0.7),
            ],
            weight_set="default",
        )

        driver = _make_graph_driver()
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        result = await handle_admin(
            "test_domain",
            {
                "subaction": "calibration_run",
                "domain_id": "test_domain",
            },
        )

        assert result["status"] == "calibration_spec_loaded"
        assert result["total_pairs"] == 1


@pytest.mark.unit
class TestScoreFeedbackSubaction:
    """Test the score_feedback admin subaction."""

    @pytest.mark.asyncio
    async def test_feedback_disabled(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.handlers._fb_settings", create=True):
            with patch("engine.config.settings.Settings") as mock_cls:
                result = await handle_admin(
                    "test_domain",
                    {
                        "subaction": "score_feedback",
                        "domain_id": "test_domain",
                    },
                )
                # By default feedback_enabled=False
                assert result["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_feedback_enabled(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.feedback_enabled = True
            result = await handle_admin(
                "test_domain",
                {
                    "subaction": "score_feedback",
                    "domain_id": "test_domain",
                },
            )

            assert result["status"] == "feedback_computed"
            assert "sample_count" in result


@pytest.mark.unit
class TestApplyWeightProposal:
    """Test the apply_weight_proposal admin subaction."""

    @pytest.mark.asyncio
    async def test_apply_disabled(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        result = await handle_admin(
            "test_tenant",
            {
                "subaction": "apply_weight_proposal",
                "proposed_weights": {"geo": 0.02},
                "current_weights": {"geo": 0.25},
            },
        )
        # Default: feedback_enabled=False
        assert result["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_apply_enabled(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.feedback_enabled = True
            result = await handle_admin(
                "test_tenant",
                {
                    "subaction": "apply_weight_proposal",
                    "proposed_weights": {"geo": 0.02},
                    "current_weights": {"geo": 0.25},
                },
            )

            assert result["status"] == "weights_applied"
            assert result["new_weights"]["geo"] == pytest.approx(0.27, abs=0.001)


@pytest.mark.unit
class TestOutcomeSyncType:
    """Test the outcome sync type (W2-02)."""

    @pytest.mark.asyncio
    async def test_outcome_sync_disabled(self):
        """When feedback_enabled=False, outcome sync should fall through to normal sync."""
        driver = _make_graph_driver()
        spec = MagicMock()
        spec.domain.id = "test_domain"
        spec.sync = None
        loader = _make_domain_loader(spec)
        init_dependencies(driver, loader)

        # With feedback disabled, "outcome" entity_type goes to normal sync path
        # which requires sync endpoints configured -> should raise
        from engine.handlers import ValidationError

        with pytest.raises(ValidationError, match="No sync endpoints configured"):
            await handle_sync(
                "test_tenant",
                {
                    "entity_type": "outcome",
                    "batch": [{"match_id": "m1", "chosen_candidate_id": "c1", "outcome": "positive"}],
                },
            )

    @pytest.mark.asyncio
    async def test_outcome_sync_enabled(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.feedback_enabled = True
            result = await handle_sync(
                "test_tenant",
                {
                    "entity_type": "outcome",
                    "batch": [
                        {"match_id": "m1", "chosen_candidate_id": "c1", "outcome": "positive"},
                        {"match_id": "m2", "chosen_candidate_id": "c2", "outcome": "negative"},
                    ],
                },
            )

            assert result["status"] == "success"
            assert result["entity_type"] == "outcome"
            assert result["synced_count"] == 2

    @pytest.mark.asyncio
    async def test_outcome_sync_invalid_outcome_skipped(self):
        driver = _make_graph_driver()
        loader = _make_domain_loader()
        init_dependencies(driver, loader)

        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.feedback_enabled = True
            result = await handle_sync(
                "test_tenant",
                {
                    "entity_type": "outcome",
                    "batch": [
                        {"match_id": "m1", "chosen_candidate_id": "c1", "outcome": "invalid"},
                        {"match_id": "m2", "chosen_candidate_id": "c2", "outcome": "positive"},
                    ],
                },
            )

            assert result["synced_count"] == 1  # only the valid one


@pytest.mark.unit
class TestFeedbackPredicate:
    """FEEDBACK-01: one effective predicate — global flag AND domain opt-in.

    Before this, ``handle_sync`` and the admin subactions read only the global
    flag while ``handle_match`` and ``handle_outcomes`` read only the domain
    flag, so the same policy had two answers. Every site now reads
    ``_feedback_effective``; these tests pin the truth table at the seams.
    """

    @pytest.mark.parametrize(
        ("global_flag", "domain_flag", "expected"),
        [(False, False, False), (False, True, False), (True, False, False), (True, True, True)],
    )
    def test_truth_table(self, global_flag: bool, domain_flag: bool, expected: bool) -> None:
        from engine.handlers import _feedback_effective

        spec = MagicMock()
        spec.feedbackloop.enabled = domain_flag
        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.feedback_enabled = global_flag
            assert _feedback_effective(spec) is expected

    @pytest.mark.asyncio
    async def test_global_on_domain_off_keeps_admin_feedback_disabled(self) -> None:
        init_dependencies(_make_graph_driver(), _make_domain_loader(domain_feedback=False))
        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.feedback_enabled = True
            feedback = await handle_admin("test_domain", {"subaction": "score_feedback", "domain_id": "test_domain"})
            applied = await handle_admin(
                "test_tenant",
                {
                    "subaction": "apply_weight_proposal",
                    "proposed_weights": {"geo": 0.02},
                    "current_weights": {"geo": 0.25},
                },
            )
        assert feedback["status"] == "disabled"
        assert applied["status"] == "disabled"
        assert "feedbackloop.enabled" in applied["message"]

    @pytest.mark.asyncio
    async def test_global_on_domain_off_sends_outcome_to_the_ordinary_sync_path(self) -> None:
        from engine.handlers import ValidationError

        init_dependencies(_make_graph_driver(), _make_domain_loader(domain_feedback=False))
        with patch("engine.config.settings.settings") as mock_settings:
            mock_settings.l9_env = "test"
            mock_settings.feedback_enabled = True
            with pytest.raises(ValidationError, match="No sync endpoints configured"):
                await handle_sync(
                    "test_tenant",
                    {
                        "entity_type": "outcome",
                        "batch": [{"match_id": "m1", "chosen_candidate_id": "c1", "outcome": "positive"}],
                    },
                )

    @pytest.mark.asyncio
    async def test_global_off_domain_on_is_still_disabled(self) -> None:
        """The domain pack cannot switch the loop on by itself; the operator flag stays authoritative."""
        init_dependencies(_make_graph_driver(), _make_domain_loader(domain_feedback=True))
        result = await handle_admin("test_domain", {"subaction": "score_feedback", "domain_id": "test_domain"})
        assert result["status"] == "disabled"

    def test_no_feedback_site_reads_a_flag_on_its_own(self) -> None:
        """Static guard: the only reader of either flag inside a handler body is the predicate."""
        import inspect

        from engine import handlers

        source = inspect.getsource(handlers)
        predicate_src = inspect.getsource(handlers._feedback_effective)
        rest = source.replace(predicate_src, "")
        # Prose (docstrings, the disabled message) may name the flags; a status
        # report may *report* the global flag; no handler body may *gate* on
        # either flag directly. ``graph_inference_feedback_enabled`` is a
        # different flag for a different loop and is not covered by FEEDBACK-01.
        gating = [
            line.strip()
            for line in rest.splitlines()
            if line.strip().startswith(("if ", "elif ", "return "))
            and ("feedbackloop.enabled" in line or ".feedback_enabled" in line)
        ]
        assert gating == [], gating


# ── RU-060 / CEG-COR-169: UNWINDMATCHSET synced_count comes from the write ──


def _match_set_spec() -> MagicMock:
    from engine.config.schema import NodeSpec, PropertySpec, SyncStrategy

    endpoint = MagicMock()
    endpoint.path = "/v1/sync/depot"
    endpoint.batchstrategy = SyncStrategy.UNWINDMATCHSET
    endpoint.targetnode = "Depot"
    endpoint.idproperty = "depot_id"
    endpoint.fieldsupdated = ["status"]
    # A node endpoint: MagicMock attributes are truthy, so name the absent edge
    # and graph-ref namespace explicitly (relationship endpoints set
    # ``targetedge``; tenant specs may declare ``refnamespace``).
    endpoint.targetedge = None
    endpoint.refnamespace = None
    spec = MagicMock()
    spec.domain.id = "test_domain"
    spec.capabilities = []
    spec.compliance = None
    spec.causal.enabled = False
    spec.sync.endpoints = [endpoint]
    spec.feedbackloop.enabled = False
    # The node the endpoint writes, with its declared types: sync validates rows
    # against them and writes only declared properties (typed sync contract).
    spec.ontology.nodes = [
        NodeSpec(
            label="Depot",
            properties=[
                PropertySpec(name="depot_id", type="string", required=True),
                PropertySpec(name="status", type="string"),
            ],
        )
    ]
    return spec


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-169")
class TestSyncMatchSetCount:
    BATCH = [{"depot_id": "d1", "status": "a"}, {"depot_id": "d2", "status": "b"}, {"depot_id": "d3", "status": "c"}]

    @pytest.mark.asyncio
    async def test_synced_count_is_the_driver_count(self):
        driver = _make_graph_driver()
        driver.execute_query = AsyncMock(return_value=[{"synced_count": 1}])
        init_dependencies(driver, _make_domain_loader(_match_set_spec()))
        with patch("engine.handlers._get_compliance_engine") as get_ce:
            get_ce.return_value.enabled = False
            get_ce.return_value.flush_audit = AsyncMock()
            get_ce.return_value.flush_audit_for_request = AsyncMock()  # RU-065 request-path flush
            result = await handle_sync("test_domain", {"entity_type": "depot", "batch": list(self.BATCH)})
        assert result["synced_count"] == 1
        assert result["unmatched_count"] == 2
        assert "RETURN count(n) AS synced_count" in driver.execute_query.call_args.kwargs["cypher"]

    @pytest.mark.asyncio
    async def test_flag_off_reports_batch_length(self, monkeypatch):
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "sync_count_from_write_result", False)
        driver = _make_graph_driver()
        init_dependencies(driver, _make_domain_loader(_match_set_spec()))
        with patch("engine.handlers._get_compliance_engine") as get_ce:
            get_ce.return_value.enabled = False
            get_ce.return_value.flush_audit = AsyncMock()
            get_ce.return_value.flush_audit_for_request = AsyncMock()  # RU-065 request-path flush
            result = await handle_sync("test_domain", {"entity_type": "depot", "batch": list(self.BATCH)})
        assert result["synced_count"] == 3
        assert "unmatched_count" not in result
