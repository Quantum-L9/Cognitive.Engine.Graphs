"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, dag, dependency]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 F04 (cases G01-G08): ``depends_on`` is a run-time
barrier, not only a registration-order sort.

The algorithms are replaced by recording coroutines, so the tests observe the
order in which graph effects would start and finish; no Neo4j is needed. The
same barrier gates cron fires and ``trigger_job``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from engine.config.schema import DomainSpec, GDSJobScheduleSpec, GDSJobSpec, GDSProjectionSpec
from engine.config.settings import settings
from engine.gds.scheduler import GDSScheduler

pytestmark = pytest.mark.unit


def _job(name: str, algorithm: str, depends_on: list[str] | None = None, *, cron: str | None = None) -> GDSJobSpec:
    return GDSJobSpec(
        name=name,
        algorithm=algorithm,
        schedule=GDSJobScheduleSpec(type="cron" if cron else "manual", cron=cron),
        projection=GDSProjectionSpec(nodelabels=["Site"], edgetypes=["LINKS"]),
        writeproperty="cluster",
        depends_on=depends_on or [],
    )


def _spec(jobs: list[GDSJobSpec]) -> DomainSpec:
    node = {"managedby": "sync", "matchdirection": "d1"}
    raw: dict[str, Any] = {
        "domain": {"id": "barrier-domain", "name": "Barrier", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {**node, "label": "Site", "candidate": True, "properties": [{"name": "sid", "type": "string"}]},
                {**node, "label": "Query", "queryentity": True, "properties": [{"name": "qid", "type": "string"}]},
            ],
            "edges": [
                {
                    "type": "LINKS",
                    "from": "Site",
                    "to": "Site",
                    "direction": "DIRECTED",
                    "category": "transaction",
                    "managedby": "sync",
                }
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


class _Recorder:
    """Replaces every algorithm runner; records start/end per job name."""

    def __init__(self, scheduler: GDSScheduler) -> None:
        self.events: list[tuple[str, str]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.fail: set[str] = set()
        for attr in (
            "_run_louvain",
            "_run_cooccurrence",
            "_run_reinforcement",
            "_run_temporal_recency",
            "_run_geoproximity",
            "_run_equipment_sync",
            "_run_causal_chain_scoring",
        ):
            setattr(scheduler, attr, self._run)

    async def _run(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        self.events.append(("start", job_spec.name))
        gate = self.gates.get(job_spec.name)
        if gate is not None:
            await gate.wait()
        if job_spec.name in self.fail:
            self.events.append(("raise", job_spec.name))
            msg = f"injected failure in {job_spec.name}"
            raise RuntimeError(msg)
        self.events.append(("end", job_spec.name))
        return {}

    def started(self, name: str) -> bool:
        return ("start", name) in self.events


def _scheduler(jobs: list[GDSJobSpec]) -> tuple[GDSScheduler, _Recorder]:
    scheduler = GDSScheduler(_spec(jobs), AsyncMock())
    scheduler.dependency_wait_timeout_sec = 0.5
    return scheduler, _Recorder(scheduler)


@pytest.fixture(autouse=True)
def _barrier_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gds_dependency_barrier_enabled", True)
    monkeypatch.setattr(settings, "gds_dag_job_identity_by_name", True)


class TestRunTimeBarrier:
    @pytest.mark.asyncio
    async def test_g01_dependent_due_at_same_instant_waits_for_producer(self) -> None:
        """G01: A and B due together; B's effect starts only after A completes."""
        a = _job("producer", "louvain", cron="0 2 * * *")
        b = _job("dependent", "cooccurrence", ["louvain"], cron="0 2 * * *")
        scheduler, rec = _scheduler([a, b])
        scheduler.dependency_wait_timeout_sec = 5.0
        scheduler.start()  # APScheduler live, no job registered: nothing fires by itself
        # The scheduler started before 02:00 and both fires are due now.
        scheduler._started_at = datetime.now(tz=UTC) - timedelta(days=2)
        rec.gates["producer"] = asyncio.Event()

        # B fires first (APScheduler gives no order between same-instant jobs).
        b_task = asyncio.create_task(scheduler.execute_job(b))
        await asyncio.sleep(0.05)
        assert not rec.started("dependent"), "B started while A's fire was due and unstarted"
        a_task = asyncio.create_task(scheduler.execute_job(a))
        await asyncio.sleep(0.05)
        assert rec.started("producer")
        assert not rec.started("dependent"), "B started while A was running"
        rec.gates["producer"].set()
        a_result, b_result = await asyncio.gather(a_task, b_task)
        scheduler.scheduler.shutdown(wait=False)

        assert a_result["status"] == "success"
        assert b_result["status"] == "success"
        assert rec.events.index(("end", "producer")) < rec.events.index(("start", "dependent"))

    @pytest.mark.asyncio
    async def test_g02_failed_producer_blocks_dependent_and_not_unrelated(self) -> None:
        """G02: A raises; B records blocked and writes nothing; C still runs."""
        a = _job("producer", "louvain")
        b = _job("dependent", "cooccurrence", ["louvain"])
        c = _job("unrelated", "geoproximity")
        scheduler, rec = _scheduler([a, b, c])
        rec.fail.add("producer")

        assert (await scheduler.execute_job(a))["status"] == "failed"
        b_result = await scheduler.execute_job(b)
        c_result = await scheduler.execute_job(c)

        assert b_result["status"] == "blocked"
        assert b_result["unsatisfied"] == ["producer: last attempt failed"]
        assert not rec.started("dependent")
        assert c_result["status"] == "success"
        assert scheduler.job_history[1]["job"] == "dependent"
        assert scheduler.job_history[1]["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_g03_manual_trigger_with_unfulfilled_dependency_is_refused_explicitly(self) -> None:
        """G03: manual trigger of B before A ever ran: explicit refusal, no bypass."""
        a = _job("producer", "louvain")
        b = _job("dependent", "cooccurrence", ["louvain"])
        scheduler, rec = _scheduler([a, b])

        result = await scheduler.trigger_job("dependent")

        assert result["status"] == "blocked"
        assert result["unsatisfied"] == ["producer: never run"]
        assert not rec.started("dependent")

        await scheduler.trigger_job("producer")
        assert (await scheduler.trigger_job("dependent"))["status"] == "success"

    @pytest.mark.asyncio
    async def test_g04_cron_dependent_waits_for_slower_producer_then_times_out(self) -> None:
        """G04: a slow A keeps B waiting; registration order is not proof of execution."""
        a = _job("producer", "louvain", cron="0 2 * * *")
        b = _job("dependent", "cooccurrence", ["louvain"], cron="0 3 * * *")
        scheduler, rec = _scheduler([a, b])
        scheduler.dependency_wait_timeout_sec = 0.2
        rec.gates["producer"] = asyncio.Event()

        a_task = asyncio.create_task(scheduler.execute_job(a))
        await asyncio.sleep(0.02)
        b_result = await scheduler.execute_job(b)

        assert b_result["status"] == "blocked"
        assert "producer: running" in b_result["unsatisfied"]
        assert not rec.started("dependent")
        rec.gates["producer"].set()
        assert (await a_task)["status"] == "success"

    @pytest.mark.asyncio
    async def test_g05_every_job_providing_the_algorithm_must_succeed(self) -> None:
        """G05: two louvain jobs; B needs both; both identities are kept."""
        a1 = _job("louvain_sites", "louvain")
        a2 = _job("louvain_regions", "louvain")
        b = _job("dependent", "cooccurrence", ["louvain"])
        scheduler, _ = _scheduler([a1, a2, b])

        await scheduler.execute_job(a1)
        partial_result = await scheduler.execute_job(b)
        assert partial_result["status"] == "blocked"
        assert partial_result["unsatisfied"] == ["louvain_regions: never run"]

        await scheduler.execute_job(a2)
        assert (await scheduler.execute_job(b))["status"] == "success"
        assert [e["job"] for e in scheduler.job_history] == [
            "louvain_sites",
            "dependent",
            "louvain_regions",
            "dependent",
        ]

    @pytest.mark.asyncio
    async def test_g08_independent_jobs_have_no_barrier(self) -> None:
        """G08: no depends_on → runs immediately even while another job runs."""
        a = _job("slow", "louvain")
        c = _job("free", "geoproximity")
        scheduler, rec = _scheduler([a, c])
        rec.gates["slow"] = asyncio.Event()

        a_task = asyncio.create_task(scheduler.execute_job(a))
        await asyncio.sleep(0.02)
        assert (await asyncio.wait_for(scheduler.execute_job(c), 0.1))["status"] == "success"
        rec.gates["slow"].set()
        await a_task

    @pytest.mark.asyncio
    async def test_partial_producer_does_not_satisfy(self) -> None:
        a = _job("producer", "louvain")
        b = _job("dependent", "cooccurrence", ["louvain"])
        scheduler, _ = _scheduler([a, b])

        async def _partial(job_spec: GDSJobSpec) -> dict[str, Any]:
            return {"status": "partial", "prune_error": "x"}

        scheduler._run_louvain = _partial  # type: ignore[method-assign]
        await scheduler.execute_job(a)
        result = await scheduler.execute_job(b)
        assert result["status"] == "blocked"
        assert result["unsatisfied"] == ["producer: last attempt partial"]


class TestStructuralRefusal:
    @pytest.mark.asyncio
    async def test_g06_unknown_dependency_is_refused_not_ignored(self) -> None:
        """G06: depends on an undeclared algorithm → refused, never cron-registered."""
        b = _job("dependent", "cooccurrence", ["pagerank"], cron="0 3 * * *")
        c = _job("free", "geoproximity", cron="0 4 * * *")
        scheduler, rec = _scheduler([b, c])

        scheduler.register_jobs()
        registered = {job.id for job in scheduler.scheduler.get_jobs()}
        result = await scheduler.trigger_job("dependent")

        assert registered == {"free"}
        assert result["status"] == "refused"
        assert result["reason"] == "depends on undeclared algorithm 'pagerank'"
        assert not rec.started("dependent")
        assert (await scheduler.trigger_job("free"))["status"] == "success"

    @pytest.mark.asyncio
    async def test_g07_cycle_members_and_downstream_refused_independent_unaffected(self) -> None:
        """G07: no fallback wave executes a cycle; the independent job still runs."""
        x = _job("x", "louvain", ["cooccurrence"], cron="0 1 * * *")
        y = _job("y", "cooccurrence", ["louvain"], cron="0 1 * * *")
        z = _job("z", "reinforcement", ["cooccurrence"], cron="0 1 * * *")
        free = _job("free", "geoproximity", cron="0 1 * * *")
        scheduler, rec = _scheduler([x, y, z, free])

        scheduler.register_jobs()
        registered = {job.id for job in scheduler.scheduler.get_jobs()}

        assert registered == {"free"}
        assert scheduler.refused_jobs["x"].startswith("dependency cycle")
        assert scheduler.refused_jobs["y"].startswith("dependency cycle")
        assert scheduler.refused_jobs["z"] == "depends on unexecutable job 'y'"
        for name in ("x", "y", "z"):
            assert (await scheduler.trigger_job(name))["status"] == "refused"
        assert rec.events == []
        assert (await scheduler.trigger_job("free"))["status"] == "success"

    @pytest.mark.asyncio
    async def test_unknown_algorithm_is_refused_not_skipped(self) -> None:
        job = _job("mystery", "pagerank")
        scheduler, _ = _scheduler([job])
        result = await scheduler.execute_job(job)
        assert result["status"] == "refused"
        assert result["reason"] == "unknown algorithm: pagerank"


class TestFlagOff:
    @pytest.mark.asyncio
    async def test_off_restores_registration_order_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_dependency_barrier_enabled", False)
        a = _job("producer", "louvain")
        b = _job("dependent", "cooccurrence", ["louvain"])
        scheduler, rec = _scheduler([a, b])
        rec.fail.add("producer")
        await scheduler.execute_job(a)
        assert (await scheduler.execute_job(b))["status"] == "success"
        assert rec.started("dependent")
        unknown = _job("mystery", "pagerank")
        assert (await scheduler.execute_job(unknown))["status"] == "skipped"

    def test_off_keeps_cycle_fallback_wave_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "gds_dependency_barrier_enabled", False)
        x = _job("x", "louvain", ["cooccurrence"], cron="0 1 * * *")
        y = _job("y", "cooccurrence", ["louvain"], cron="0 1 * * *")
        scheduler, _ = _scheduler([x, y])
        scheduler.register_jobs()
        assert {job.id for job in scheduler.scheduler.get_jobs()} == {"x", "y"}

    def test_flag_default_on(self) -> None:
        from engine.config.settings import Settings

        assert Settings.model_fields["gds_dependency_barrier_enabled"].default is True


class TestReviewFindings:
    """CodeRabbit on #543: provider in-flight state, cancellation, identity, registration."""

    @pytest.mark.asyncio
    async def test_provider_waiting_on_its_own_dependency_is_in_flight(self) -> None:
        """C depends on B, B on A: while B waits for A, C must not read B's old success."""
        a = _job("a", "louvain")
        b = _job("b", "cooccurrence", ["louvain"])
        c = _job("c", "reinforcement", ["cooccurrence"])
        scheduler, rec = _scheduler([a, b, c])
        scheduler.dependency_wait_timeout_sec = 5.0
        await scheduler.execute_job(a)
        await scheduler.execute_job(b)  # B succeeded once
        rec.gates["a"] = asyncio.Event()
        a_task = asyncio.create_task(scheduler.execute_job(a))
        await asyncio.sleep(0.02)
        b_task = asyncio.create_task(scheduler.execute_job(b))  # waits on A
        await asyncio.sleep(0.02)
        c_task = asyncio.create_task(scheduler.execute_job(c))
        await asyncio.sleep(0.05)
        assert rec.events.count(("start", "c")) == 0, "C started on B's previous success"
        rec.gates["a"].set()
        await asyncio.gather(a_task, b_task, c_task)
        assert rec.events.index(("end", "b"), rec.events.index(("end", "a"), 4)) < rec.events.index(("start", "c"))

    @pytest.mark.asyncio
    async def test_concurrent_attempts_of_one_provider_keep_it_running(self) -> None:
        a = _job("a", "louvain")
        b = _job("b", "cooccurrence", ["louvain"])
        scheduler, _ = _scheduler([a, b])
        scheduler.dependency_wait_timeout_sec = 0.2
        first, second = asyncio.Event(), asyncio.Event()
        gates = iter([first, second])

        async def _run(job_spec: GDSJobSpec) -> dict[str, Any]:
            await next(gates).wait()
            return {}

        scheduler._run_louvain = _run  # type: ignore[method-assign]
        t1 = asyncio.create_task(scheduler.execute_job(a))
        t2 = asyncio.create_task(scheduler.execute_job(a))
        await asyncio.sleep(0.02)
        first.set()
        await t1
        result = await scheduler.execute_job(b)
        assert result["status"] == "blocked"
        assert result["unsatisfied"] == ["a: running"]
        second.set()
        await t2

    @pytest.mark.asyncio
    async def test_cancelled_retry_invalidates_previous_success(self) -> None:
        a = _job("a", "louvain")
        b = _job("b", "cooccurrence", ["louvain"])
        scheduler, rec = _scheduler([a, b])
        await scheduler.execute_job(a)
        rec.gates["a"] = asyncio.Event()
        retry = asyncio.create_task(scheduler.execute_job(a))
        await asyncio.sleep(0.02)
        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry
        result = await scheduler.execute_job(b)
        assert result["status"] == "blocked"
        assert result["unsatisfied"] == ["a: last attempt cancelled"]
        assert scheduler.job_history[-2]["status"] == "cancelled"
        rec.gates["a"].set()
        await scheduler.execute_job(a)
        assert (await scheduler.execute_job(b))["status"] == "success"

    @pytest.mark.asyncio
    async def test_duplicate_job_names_are_refused(self) -> None:
        geo = _job("shared", "geoproximity")
        louvain = _job("shared", "louvain")
        b = _job("b", "cooccurrence", ["louvain"])
        scheduler, rec = _scheduler([geo, louvain, b])
        assert scheduler.refused_jobs["shared"] == "job name declared 2 times; dependency state needs a unique name"
        assert scheduler.refused_jobs["b"] == "depends on unexecutable job 'shared'"
        assert (await scheduler.trigger_job("shared"))["status"] == "refused"
        assert rec.events == []

    def test_unknown_algorithm_cron_job_is_not_registered(self) -> None:
        mystery = _job("mystery", "pagerank", cron="0 3 * * *")
        free = _job("free", "geoproximity", cron="0 4 * * *")
        scheduler, _ = _scheduler([mystery, free])
        scheduler.register_jobs()
        assert {job.id for job in scheduler.scheduler.get_jobs()} == {"free"}
        assert "mystery" not in scheduler.refused_jobs  # dependents of it block, not refuse

    def test_barrier_registers_every_provider_with_algorithm_keyed_dag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "gds_dag_job_identity_by_name", False)
        a1 = _job("louvain_a", "louvain", cron="0 1 * * *")
        a2 = _job("louvain_b", "louvain", cron="0 1 * * *")
        b = _job("b", "cooccurrence", ["louvain"], cron="0 2 * * *")
        scheduler, _ = _scheduler([a1, a2, b])
        scheduler.register_jobs()
        assert {job.id for job in scheduler.scheduler.get_jobs()} == {"louvain_a", "louvain_b", "b"}
