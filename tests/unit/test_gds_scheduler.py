"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds]
owner: engine-team
status: active
--- /L9_META ---

Tests for engine.gds.scheduler — GDSScheduler.

Covers:
- Job registration (cron + manual)
- Algorithm dispatch (louvain, cooccurrence, reinforcement, temporalrecency,
  geoproximity, equipmentsync)
- Unknown algorithm handling
- Error handling / job history
- Manual trigger (trigger_job)
- Lifecycle (start / shutdown)

All Neo4j calls mocked via AsyncMock — no container required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from engine.config.schema import (
    DomainSpec,
    GDSJobScheduleSpec,
    GDSJobSpec,
    GDSProjectionSpec,
)
from engine.gds.scheduler import GDSScheduler

# ── Helpers ───────────────────────────────────────────────


def _job(
    name: str,
    algorithm: str,
    *,
    schedule_type: str = "cron",
    cron: str | None = "0 2 * * *",
    node_labels: list[str] | None = None,
    edge_types: list[str] | None = None,
    writeproperty: str | None = None,
    sourceedge: str | None = None,
    writeedge: str | None = None,
    writeto: str | None = None,
    writeproperties: list[dict] | None = None,
) -> GDSJobSpec:
    return GDSJobSpec(
        name=name,
        algorithm=algorithm,
        schedule=GDSJobScheduleSpec(type=schedule_type, cron=cron if schedule_type == "cron" else None),
        projection=GDSProjectionSpec(
            nodelabels=node_labels or ["Site"],
            edgetypes=edge_types or ["INTERACTED_WITH"],
        ),
        writeproperty=writeproperty,
        sourceedge=sourceedge,
        writeedge=writeedge,
        writeto=writeto,
        writeproperties=writeproperties,
    )


# Synthetic vocabulary: these names belong to the test domain, not to the engine.
_REINFORCEMENT_JOB = {
    "sourceedge": "INTERACTED_WITH",
    "writeedge": "REINFORCED_BY",
    "writeproperties": [{"negative_writeedge": "DISCOURAGED_BY"}],
}
_TEMPORAL_JOB = {
    "sourceedge": "INTERACTED_WITH",
    "writeedge": "RECENTLY_INTERACTED_WITH",
    "writeproperties": [{"timestamp_property": "last_seen_at"}],
}
_PROPERTY_EDGE_JOB = {
    "writeedge": "OFFERS",
    "writeto": "ServiceType",
    "writeproperties": [{"property_mappings": {"has_dock": "dock", "has_crane": "crane"}}],
}


def _cypher_sent(driver: AsyncMock) -> str:
    return "\n".join(str(call.args[0]) for call in driver.execute_query.call_args_list)


def _minimal_spec(jobs: list[GDSJobSpec]) -> DomainSpec:
    raw = {
        "domain": {"id": "test-domain", "name": "Test", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {
                    "label": "Site",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "site_id", "type": "int", "required": True}],
                },
                {
                    "label": "Query",
                    "managedby": "api",
                    "queryentity": True,
                    "matchdirection": "d1",
                    "properties": [{"name": "query_id", "type": "int", "required": True}],
                },
            ],
            "edges": [
                {
                    "type": "INTERACTED_WITH",
                    "from": "Site",
                    "to": "Site",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "sync",
                },
            ],
        },
        "matchentities": {
            "candidate": [{"label": "Site", "matchdirection": "d1"}],
            "queryentity": [{"label": "Query", "matchdirection": "d1"}],
        },
        "queryschema": {"matchdirections": ["d1"], "fields": []},
        "traversal": {"steps": []},
        "gates": [],
        "scoring": {"dimensions": []},
        "gdsjobs": [j.model_dump() for j in jobs],
    }
    return DomainSpec(**raw)


def _mock_driver(return_data: list | None = None) -> AsyncMock:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=return_data or [])
    return driver


# ── Job Registration ──────────────────────────────────────


class TestRegistration:
    def test_register_cron_jobs(self):
        jobs = [_job("louvain_nightly", "louvain")]
        spec = _minimal_spec(jobs)
        driver = _mock_driver()
        scheduler = GDSScheduler(spec, driver)

        with patch.object(scheduler.scheduler, "add_job") as mock_add:
            scheduler.register_jobs()
            mock_add.assert_called_once()
            call_kwargs = mock_add.call_args
            assert call_kwargs.kwargs["id"] == "louvain_nightly"

    def test_register_manual_job_no_add(self):
        jobs = [_job("manual_job", "louvain", schedule_type="manual")]
        spec = _minimal_spec(jobs)
        driver = _mock_driver()
        scheduler = GDSScheduler(spec, driver)

        with patch.object(scheduler.scheduler, "add_job") as mock_add:
            scheduler.register_jobs()
            mock_add.assert_not_called()

    def test_cron_missing_expression_raises(self):
        job = _job("bad", "louvain", schedule_type="cron", cron=None)
        # Override to force None cron
        job.schedule = GDSJobScheduleSpec(type="cron", cron=None)
        spec = _minimal_spec([job])
        driver = _mock_driver()
        scheduler = GDSScheduler(spec, driver)
        with pytest.raises(ValueError, match="cron expression required"):
            scheduler._register_cron_job(job)

    @pytest.mark.asyncio
    async def test_same_algorithm_cron_jobs_all_register(self):
        """RU-096 / CEG-COR-036: two louvain cron jobs are two APScheduler jobs."""
        jobs = [_job("louvain_by_material", "louvain"), _job("louvain_by_polymer", "louvain")]
        scheduler = GDSScheduler(_minimal_spec(jobs), _mock_driver())
        scheduler.register_jobs()
        scheduler.start()
        try:
            ids = sorted(job.id for job in scheduler.scheduler.get_jobs())
        finally:
            scheduler.scheduler.shutdown(wait=False)
        assert ids == ["louvain_by_material", "louvain_by_polymer"]

    def test_register_log_reports_jobs_actually_registered(self, caplog):
        jobs = [
            _job("louvain_by_material", "louvain"),
            _job("louvain_by_polymer", "louvain"),
            _job("manual_one", "louvain", schedule_type="manual"),
        ]
        scheduler = GDSScheduler(_minimal_spec(jobs), _mock_driver())
        with patch.object(scheduler.scheduler, "add_job") as mock_add, caplog.at_level("INFO"):
            scheduler.register_jobs()
        assert mock_add.call_count == 2
        assert "Registered 2 GDS cron jobs" in caplog.text


# ── Algorithm Dispatch ────────────────────────────────────


class TestAlgorithmDispatch:
    @pytest.mark.asyncio
    async def test_louvain_executes_project_and_write(self):
        driver = _mock_driver([{"communityCount": 5, "modularity": 0.42}])
        job = _job("test_louvain", "louvain")
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)

        assert result["status"] == "success"
        assert driver.execute_query.call_count >= 2  # project + louvain + drop
        assert "duration_sec" in result

    @pytest.mark.asyncio
    async def test_cooccurrence_requires_sourceedge_and_writeedge(self):
        driver = _mock_driver()
        job = _job("test_cooc", "cooccurrence", sourceedge=None, writeedge=None)
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "failed"
        assert "sourceedge" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_cooccurrence_happy_path(self):
        driver = _mock_driver([{"edges_created": 15}])
        job = _job("test_cooc", "cooccurrence", sourceedge="ACCEPTS_POLYMER", writeedge="CO_PROCESSES")
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "success"
        # RU-098 / CEG-COR-168: per-node keep sets are ordered by weight (with a
        # deterministic tie-break) before collect; an edge is deleted only when
        # both endpoints prune (degree > 10) and neither keeps it.
        prune = str(driver.execute_query.call_args_list[1].args[0])
        assert "ORDER BY r.weight DESC, elementId(r)" in prune
        assert prune.index("ORDER BY r.weight DESC") < prune.index("collect(r)")
        assert "WHERE degree > 10" in prune
        assert "WHERE votes = 2" in prune
        assert ":CO_PROCESSES" in prune

    @pytest.mark.asyncio
    async def test_cooccurrence_flag_off_restores_legacy_prune(self, monkeypatch):
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "gds_cooccurrence_deterministic_prune", False)
        driver = _mock_driver([{"edges_created": 15}])
        job = _job("test_cooc", "cooccurrence", sourceedge="ACCEPTS_POLYMER", writeedge="CO_PROCESSES")
        scheduler = GDSScheduler(_minimal_spec([job]), driver)
        result = await scheduler.execute_job(job)
        assert result["pruning_alpha"] == 0.86
        prune = str(driver.execute_query.call_args_list[1].args[0])
        assert "ORDER BY" not in prune
        assert "edges[keep_count..] AS to_prune" in prune

    def test_cooccurrence_flag_default_on(self):
        from engine.config.settings import Settings

        assert Settings().gds_cooccurrence_deterministic_prune is True

    @pytest.mark.asyncio
    async def test_reinforcement_creates_accepted_and_rejected(self):
        driver = _mock_driver([{"accepted_edges": 10}])
        # Override return for second call
        driver.execute_query = AsyncMock(
            side_effect=[
                [{"accepted_edges": 10}],  # accepted
                [{"rejected_edges": 3}],  # rejected
                [],  # prune accepted
                [],  # prune rejected
            ]
        )
        job = _job("test_reinf", "reinforcement", **_REINFORCEMENT_JOB)
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "success"
        assert result["accepted_edges"] == 10
        assert result["rejected_edges"] == 3
        sent = _cypher_sent(driver)
        assert ":INTERACTED_WITH" in sent
        assert ":REINFORCED_BY" in sent
        assert ":DISCOURAGED_BY" in sent
        # Outcome values travel as parameters, never as quoted literals (C-009).
        assert "'success'" not in sent
        assert driver.execute_query.call_args_list[0].kwargs["parameters"]["success_value"] == "success"

    @pytest.mark.asyncio
    async def test_reinforcement_optional_aggregates_follow_declared_properties(self):
        driver = _mock_driver([{"accepted_edges": 1}])
        job = _job(
            "test_reinf_agg",
            "reinforcement",
            sourceedge="INTERACTED_WITH",
            writeedge="REINFORCED_BY",
            writeproperties=[
                {"negative_writeedge": "DISCOURAGED_BY"},
                {"value_property": "amount", "quality_property": "rating", "reason_property": "why"},
            ],
        )
        scheduler = GDSScheduler(_minimal_spec([job]), driver)
        await scheduler.execute_job(job)
        sent = _cypher_sent(driver)
        assert "t.amount" in sent
        assert "t.rating" in sent
        assert "t.why" in sent

    @pytest.mark.asyncio
    async def test_reinforcement_without_declared_edges_fails_explicitly(self):
        """No declared vocabulary means an explicit failure — never a built-in recipe."""
        driver = _mock_driver()
        job = _job("bare_reinf", "reinforcement")
        scheduler = GDSScheduler(_minimal_spec([job]), driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "failed"
        assert "requires 'sourceedge'" in result["error"]
        driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_reinforcement_requires_negative_edge(self):
        driver = _mock_driver()
        job = _job("half_reinf", "reinforcement", sourceedge="INTERACTED_WITH", writeedge="REINFORCED_BY")
        scheduler = GDSScheduler(_minimal_spec([job]), driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "failed"
        assert "negative_writeedge" in result["error"]
        driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporal_recency_creates_and_prunes(self):
        driver = _mock_driver()
        # RU-097: create, refresh (recency of every existing overlay edge), prune.
        driver.execute_query = AsyncMock(
            side_effect=[
                [{"edges_created": 20}],
                [{"edges_refreshed": 25}],
                [{"edges_pruned": 5}],
            ]
        )
        job = _job("test_temporal", "temporalrecency", **_TEMPORAL_JOB)
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "success"
        assert result["edges_created"] == 20
        assert result["edges_pruned"] == 5
        sent = _cypher_sent(driver)
        assert ":RECENTLY_INTERACTED_WITH" in sent
        assert "t.last_seen_at" in sent

        # RU-097 / CEG-COR-037: aggregate per (src, dst) before MERGE, refresh
        # existing overlay edges, then prune on score OR age.
        create, refresh, prune = (str(c.args[0]) for c in driver.execute_query.call_args_list)
        assert "max(t.last_seen_at)" in create
        assert create.index("max(t.last_seen_at)") < create.index("MERGE")
        assert "r.last_event_date" in refresh
        assert "SET r.recency_score" in refresh
        assert "MERGE" not in refresh
        assert "DELETE" not in refresh
        assert "$prune_below" in prune
        assert "$max_age_days" in prune
        assert "DELETE r" in prune
        for call in driver.execute_query.call_args_list:
            assert call.kwargs["parameters"] == {"half_life": 90.0, "max_age_days": 207.0, "prune_below": 0.1}

    @pytest.mark.asyncio
    async def test_temporal_recency_recent_count_aggregates_per_pair(self):
        driver = _mock_driver()
        driver.execute_query = AsyncMock(side_effect=[[{"edges_created": 1}], [], [{"edges_pruned": 0}]])
        job = _job(
            "test_temporal_count",
            "temporalrecency",
            sourceedge="INTERACTED_WITH",
            writeedge="RECENTLY_INTERACTED_WITH",
            writeproperties=[{"timestamp_property": "last_seen_at"}, {"count_property": "hits"}],
        )
        scheduler = GDSScheduler(_minimal_spec([job]), driver)
        await scheduler.execute_job(job)
        create = str(driver.execute_query.call_args_list[0].args[0])
        assert "sum(CASE WHEN age_days <= $half_life THEN coalesce(t.hits, 1) ELSE 0 END)" in create

    @pytest.mark.asyncio
    async def test_temporal_recency_flag_off_restores_legacy_cypher(self, monkeypatch):
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "gds_temporal_recency_decay_existing", False)
        driver = _mock_driver()
        driver.execute_query = AsyncMock(side_effect=[[{"edges_created": 20}], [{"edges_pruned": 5}]])
        job = _job("test_temporal", "temporalrecency", **_TEMPORAL_JOB)
        scheduler = GDSScheduler(_minimal_spec([job]), driver)
        result = await scheduler.execute_job(job)
        assert result["edges_created"] == 20
        assert result["edges_pruned"] == 5
        create, prune = (str(c.args[0]) for c in driver.execute_query.call_args_list)
        assert "max(" not in create
        assert "SET r.last_event_date = t.last_seen_at" in create
        assert "$max_age_days" not in prune

    def test_temporal_recency_flag_default_on(self):
        from engine.config.settings import Settings

        assert Settings().gds_temporal_recency_decay_existing is True

    @pytest.mark.asyncio
    async def test_temporal_recency_requires_timestamp_property(self):
        driver = _mock_driver()
        job = _job("bare_temporal", "temporalrecency", sourceedge="INTERACTED_WITH", writeedge="RECENT")
        scheduler = GDSScheduler(_minimal_spec([job]), driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "failed"
        assert "timestamp_property" in result["error"]
        driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_geoproximity_creates_proximity_edges(self):
        driver = _mock_driver([{"edges_created": 8}])
        job = _job("test_geo", "geoproximity")
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "success"
        assert ":COLOCATED_WITH" in _cypher_sent(driver)

    @pytest.mark.asyncio
    async def test_geoproximity_honours_declared_edge_and_coordinates(self):
        driver = _mock_driver([{"edges_created": 2}])
        job = _job(
            "test_geo_cfg",
            "geoproximity",
            writeedge="NEAR",
            writeproperties=[{"lat_property": "latitude", "lon_property": "longitude", "id_property": "site_id"}],
        )
        scheduler = GDSScheduler(_minimal_spec([job]), driver)
        await scheduler.execute_job(job)
        sent = _cypher_sent(driver)
        assert ":NEAR" in sent
        assert "a.latitude" in sent
        assert "a.site_id < b.site_id" in sent

    @pytest.mark.asyncio
    async def test_property_edge_sync_materializes_declared_edges(self):
        driver = _mock_driver([{"edges_created": 24}])
        job = _job("test_prop_edges", "equipmentsync", **_PROPERTY_EDGE_JOB)
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "success"
        sent = _cypher_sent(driver)
        assert "(e:ServiceType {name:" in sent
        assert ":OFFERS" in sent
        assert "f.has_dock" in sent
        params = driver.execute_query.call_args.kwargs["parameters"]
        assert set(params.values()) == {"dock", "crane"}

    @pytest.mark.asyncio
    async def test_property_edge_sync_has_no_built_in_mapping_table(self):
        driver = _mock_driver()
        job = _job("bare_prop_edges", "equipmentsync", writeedge="OFFERS", writeto="ServiceType")
        scheduler = GDSScheduler(_minimal_spec([job]), driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "failed"
        assert "property_mappings" in result["error"]
        driver.execute_query.assert_not_called()

    def test_candidate_label_requires_a_declaration(self):
        """No projection label, no candidate entity, no candidate node: explicit failure."""
        from unittest.mock import MagicMock

        domain_spec = MagicMock()
        domain_spec.matchentities.candidate = []
        domain_spec.ontology.nodes = []
        job = GDSJobSpec(
            name="unlabelled",
            algorithm="geoproximity",
            schedule=GDSJobScheduleSpec(type="manual"),
            projection=GDSProjectionSpec(nodelabels=[], edgetypes=[]),
        )
        scheduler = GDSScheduler(domain_spec, _mock_driver())

        with pytest.raises(ValueError, match="no node label declared"):
            scheduler._get_candidate_label(job)

    @pytest.mark.asyncio
    async def test_unknown_algorithm_skipped(self):
        driver = _mock_driver()
        job = _job("test_unknown", "nonexistent_algo")
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "skipped"
        assert "unknown algorithm" in result["reason"]


# ── Error Handling ────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_execute_job_catches_exception(self):
        driver = _mock_driver()
        driver.execute_query = AsyncMock(side_effect=RuntimeError("Neo4j down"))
        job = _job("test_fail", "louvain")
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.execute_job(job)
        assert result["status"] == "failed"
        assert "Neo4j down" in result["error"]

    @pytest.mark.asyncio
    async def test_job_history_records_failures(self):
        driver = _mock_driver()
        driver.execute_query = AsyncMock(side_effect=RuntimeError("boom"))
        job = _job("history_test", "louvain")
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        await scheduler.execute_job(job)
        assert len(scheduler.job_history) == 1
        assert scheduler.job_history[0]["status"] == "failed"
        assert scheduler.job_history[0]["job"] == "history_test"

    @pytest.mark.asyncio
    async def test_job_history_records_successes(self):
        driver = _mock_driver([{"edges_created": 1}])
        job = _job("ok_job", "equipmentsync", **_PROPERTY_EDGE_JOB)
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        await scheduler.execute_job(job)
        assert len(scheduler.job_history) == 1
        assert scheduler.job_history[0]["status"] == "success"


# ── Manual Trigger ────────────────────────────────────────


class TestManualTrigger:
    @pytest.mark.asyncio
    async def test_trigger_existing_job(self):
        driver = _mock_driver([{"edges_created": 5}])
        job = _job("my_job", "equipmentsync", schedule_type="manual", **_PROPERTY_EDGE_JOB)
        spec = _minimal_spec([job])
        scheduler = GDSScheduler(spec, driver)

        result = await scheduler.trigger_job("my_job")
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_trigger_unknown_job_raises(self):
        spec = _minimal_spec([])
        driver = _mock_driver()
        scheduler = GDSScheduler(spec, driver)

        with pytest.raises(ValueError, match="not found"):
            await scheduler.trigger_job("ghost")


# ── Lifecycle ─────────────────────────────────────────────


class TestLifecycle:
    def test_start_and_shutdown(self):
        spec = _minimal_spec([])
        driver = _mock_driver()
        scheduler = GDSScheduler(spec, driver)

        with (
            patch.object(scheduler.scheduler, "start") as mock_start,
            patch.object(scheduler.scheduler, "shutdown") as mock_stop,
        ):
            scheduler.start()
            mock_start.assert_called_once()
            scheduler.shutdown()
            mock_stop.assert_called_once()


# ── RU-100 / CEG-COR-086: history timestamps in timezone-aware UTC ────────


@pytest.fixture
def process_tz(monkeypatch):
    """Set the process timezone for one test and restore it afterwards."""
    import time

    def _set(tz: str) -> None:
        monkeypatch.setenv("TZ", tz)
        time.tzset()

    yield _set
    monkeypatch.undo()
    time.tzset()


class TestHistoryTimestamps:
    @staticmethod
    def _apparent_age_seconds(ts_str: str) -> float:
        """The consumer conversion of engine/handlers.py gds_health / engine/boot.py."""
        from datetime import UTC, datetime

        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (datetime.now(tz=UTC) - ts).total_seconds()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tz", ["America/Los_Angeles", "Asia/Tokyo", "UTC"])
    async def test_history_timestamp_is_utc_aware(self, process_tz, tz):
        from datetime import UTC, datetime

        process_tz(tz)
        job = _job("noop", "noop_algo", schedule_type="manual")
        scheduler = GDSScheduler(_minimal_spec([job]), _mock_driver())
        await scheduler.execute_job(job)
        ts_str = scheduler.job_history[-1]["timestamp"]
        assert datetime.fromisoformat(ts_str).utcoffset() is not None
        assert datetime.fromisoformat(ts_str).tzinfo == UTC
        assert abs(self._apparent_age_seconds(ts_str)) < 60

    @pytest.mark.asyncio
    async def test_flag_off_restores_naive_local_timestamps(self, process_tz, monkeypatch):
        from datetime import datetime

        from engine.config.settings import settings

        monkeypatch.setattr(settings, "gds_history_utc_timestamps", False)
        process_tz("Asia/Tokyo")
        job = _job("noop", "noop_algo", schedule_type="manual")
        scheduler = GDSScheduler(_minimal_spec([job]), _mock_driver())
        await scheduler.execute_job(job)
        assert datetime.fromisoformat(scheduler.job_history[-1]["timestamp"]).tzinfo is None

    def test_flag_default_on(self):
        from engine.config.settings import Settings

        assert Settings().gds_history_utc_timestamps is True


# ── Stable Louvain (S2-15) contract ───────────────────────


class TestStableLouvainContract:
    """RU-099 / CEG-COR-085 (DEC-39 option A): the multi-run path is honest.

    N runs each write the same property, so the persisted labels are the last
    successful run's; the result reports averaged metrics and says, through
    ``consensus=False``, that no per-node consensus was computed.
    """

    @pytest.mark.asyncio
    async def test_stability_runs_report_no_consensus(self):
        driver = _mock_driver([{"communityCount": 4, "modularity": 0.40}])
        job = _job("stable_louvain", "louvain").model_copy(update={"stability_runs": 3})
        scheduler = GDSScheduler(_minimal_spec([job]), driver)

        result = await scheduler.execute_job(job)

        assert result["status"] == "success"
        assert result["consensus"] is False
        assert result["stability_runs"] == 3
        assert result["communities"] == 4
        assert result["modularity"] == pytest.approx(0.40)
        assert result["stability_coefficient"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_single_run_path_has_no_consensus_key(self):
        driver = _mock_driver([{"communityCount": 4, "modularity": 0.40}])
        job = _job("plain_louvain", "louvain")
        scheduler = GDSScheduler(_minimal_spec([job]), driver)

        result = await scheduler.execute_job(job)

        assert "consensus" not in result
        assert "stability_runs" not in result

    def test_docstring_does_not_claim_majority_consensus(self):
        doc = (GDSScheduler._run_stable_louvain.__doc__ or "").lower()
        assert "majority" not in doc
        assert "last successful run" in doc
