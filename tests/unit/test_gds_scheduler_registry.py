"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, lifecycle]
owner: engine-team
status: active
--- /L9_META ---

RU-007 / CEG-COR-016 (RG-009): one GDS scheduler registry in EngineState.

Boot used to keep its schedulers in ``GraphLifecycle._schedulers`` (never
stopped), and the first admin ``gds_*`` call started a second scheduler for the
same domain (cron jobs ran twice, history read empty, ``GDS_ENABLED=false`` was
ignored). With ``gds_shared_scheduler_registry`` on, boot registers into
``get_state().gds_schedulers``, admin reuses that instance, and
``EngineState.shutdown`` stops every scheduler.

No Neo4j: the driver is a mock and the domain loader returns one in-memory spec.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine import boot
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.gds.scheduler import GDSScheduler
from engine.handlers import handle_admin
from engine.state import _reset_singleton, get_state
from tests.unit.test_gds_scheduler import _job, _minimal_spec

DOMAIN_ID = "test-domain"
CRON_JOB = "louvain_community"


def _spec() -> DomainSpec:
    job = _job(
        CRON_JOB,
        "louvain",
        node_labels=["Site"],
        edge_types=["INTERACTED_WITH"],
        writeproperty="community_id",
    )
    return _minimal_spec([job], domain_id=DOMAIN_ID)


class _Probe:
    """Records every started GDSScheduler and every cron registration."""

    def __init__(self) -> None:
        self.started: list[GDSScheduler] = []
        self.created: list[GDSScheduler] = []
        self.registrations: Counter[str] = Counter()

    async def still_running(self) -> int:
        # AsyncIOScheduler.shutdown completes on the next loop iteration.
        await asyncio.sleep(0)
        return sum(1 for s in self.created if s.scheduler.running)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Probe]:
    rec = _Probe()
    original_init = GDSScheduler.__init__
    original_start = GDSScheduler.start
    original_register = GDSScheduler._register_cron_job

    def _init(self: GDSScheduler, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        rec.created.append(self)

    def _start(self: GDSScheduler) -> None:
        rec.started.append(self)
        original_start(self)

    def _register(self: GDSScheduler, job_spec: Any) -> None:
        rec.registrations[job_spec.name] += 1
        original_register(self, job_spec)

    monkeypatch.setattr(GDSScheduler, "__init__", _init)
    monkeypatch.setattr(GDSScheduler, "start", _start)
    monkeypatch.setattr(GDSScheduler, "_register_cron_job", _register)
    yield rec
    for sched in rec.created:
        if sched.scheduler.running:
            sched.scheduler.shutdown(wait=False)


@pytest.fixture
def lifecycle_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    monkeypatch.setattr(settings, "sdk_participation_enabled", True)
    monkeypatch.setattr(settings, "postgres_dsn", None)
    monkeypatch.setattr(settings, "gds_enabled", True)
    monkeypatch.delenv("TENANT_ALLOWLIST", raising=False)
    spec = _spec()
    loader = MagicMock()
    loader.list_domains = MagicMock(return_value=[DOMAIN_ID])
    loader.load_domain = MagicMock(return_value=spec)
    driver = MagicMock()
    driver.connect = AsyncMock()
    driver.close = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[])
    _reset_singleton()
    with (
        patch.object(boot, "GraphDriver", return_value=driver),
        patch.object(boot, "DomainPackLoader", return_value=loader),
    ):
        yield loader
    _reset_singleton()


async def _gds_status() -> dict[str, Any]:
    return await handle_admin(DOMAIN_ID, {"subaction": "gds_status", "domain_id": DOMAIN_ID})


@pytest.mark.unit
class TestSharedRegistryOn:
    async def test_boot_and_admin_share_one_scheduler_and_shutdown_stops_it(
        self, monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            assert list(get_state().gds_schedulers) == [DOMAIN_ID]
            await _gds_status()
            assert len(probe.started) == 1
            assert probe.registrations[CRON_JOB] == 1
            assert get_state().gds_schedulers[DOMAIN_ID] is probe.started[0]
        finally:
            await lifecycle.shutdown()
        assert await probe.still_running() == 0

    async def test_gds_status_reports_the_boot_scheduler_history(
        self, monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            boot_scheduler = get_state().gds_schedulers[DOMAIN_ID]
            boot_scheduler._job_history.append(
                {
                    "job": CRON_JOB,
                    "algorithm": "louvain",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "status": "success",
                }
            )
            result = await _gds_status()
        finally:
            await lifecycle.shutdown()
        assert result["history_count"] == 1
        assert result["last_runs"][CRON_JOB]["status"] == "success"

    async def test_health_probe_reads_the_shared_registry(
        self, monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            assert lifecycle._schedulers == []
            get_state().gds_schedulers[DOMAIN_ID]._job_history.append(
                {"job": CRON_JOB, "algorithm": "louvain", "timestamp": "2020-01-01T00:00:00+00:00"}
            )
            result = await lifecycle._gds_health_check()
        finally:
            await lifecycle.shutdown()
        # The probe sees the registered scheduler's stale run.
        assert result["gds"] == "degraded: stale jobs: louvain"

    async def test_gds_disabled_admin_starts_no_apscheduler(
        self,
        monkeypatch: pytest.MonkeyPatch,
        probe: _Probe,
        lifecycle_env: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
        monkeypatch.setattr(settings, "gds_enabled", False)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            assert get_state().gds_schedulers == {}
            assert not hasattr(lifecycle, "_gds_health_check")
            result = await _gds_status()
            assert result["status"] == "ok"
            assert probe.started == []
            assert await probe.still_running() == 0
            # Jobs are registered for manual trigger; the scheduler is reused.
            assert probe.registrations[CRON_JOB] == 1
            await _gds_status()
            assert len(probe.created) == 1
        finally:
            with caplog.at_level(logging.WARNING, logger="engine.state"):
                await lifecycle.shutdown()
        assert get_state().gds_schedulers == {}
        assert not [r for r in caplog.records if "shutdown error" in r.getMessage()]

    async def test_gds_disabled_manual_trigger_still_runs(
        self, monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", True)
        monkeypatch.setattr(settings, "gds_enabled", False)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            with patch.object(GDSScheduler, "execute_job", AsyncMock(return_value={"status": "success"})) as run:
                result = await handle_admin(
                    DOMAIN_ID,
                    {"subaction": "gds_trigger", "domain_id": DOMAIN_ID, "job_name": CRON_JOB},
                )
        finally:
            await lifecycle.shutdown()
        assert result["status"] == "triggered"
        assert run.await_count == 1
        assert probe.started == []


@pytest.mark.unit
class TestSharedRegistryOff:
    """Flag off: the pre-fix boot-local list and admin duplicate, verbatim."""

    async def test_off_keeps_boot_local_list_and_admin_duplicate(
        self, monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", False)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            assert get_state().gds_schedulers == {}
            assert len(lifecycle._schedulers) == 1
            await _gds_status()
            assert len(probe.started) == 2
            assert probe.registrations[CRON_JOB] == 2
        finally:
            await lifecycle.shutdown()
        # The boot scheduler is never stopped (the defect the flag restores).
        assert await probe.still_running() == 1

    async def test_off_gds_disabled_admin_still_starts(
        self, monkeypatch: pytest.MonkeyPatch, probe: _Probe, lifecycle_env: MagicMock
    ) -> None:
        monkeypatch.setattr(settings, "gds_shared_scheduler_registry", False)
        monkeypatch.setattr(settings, "gds_enabled", False)
        lifecycle = boot.GraphLifecycle()
        await lifecycle.startup()
        try:
            await _gds_status()
            assert len(probe.started) == 1
        finally:
            await lifecycle.shutdown()
        assert await probe.still_running() == 0
