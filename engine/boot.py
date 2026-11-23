"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [engine, boot, lifecycle]
owner: engine-team
status: active
--- /L9_META ---

engine/boot.py — Graph Cognitive Engine LifecycleHook

Concrete implementation of chassis.chassis_app.LifecycleHook for the
Graph Cognitive Engine.  This is the ONLY file that couples
engine internals to the chassis contract.

    L9_LIFECYCLE_HOOK=engine.boot:GraphLifecycle

NOTE: chassis/engine_boot.py was removed (was a stale duplicate of this file).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from chassis.chassis_app import LifecycleHook
from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import init_dependencies
from engine.state import get_state

logger = logging.getLogger(__name__)

# C-22: the scoring weight ceiling is checked per domain, per match direction,
# when a spec loads (DomainSpec cross-reference validation). Boot asserts no
# engine-wide weight set: scoring dimensions are the domain's declaration.


class GraphLifecycle(LifecycleHook):
    """
    Wires Neo4j, domain packs, handler dependencies, and GDS scheduler
    lifecycle for the L9 Graph Cognitive Engine.
    """

    def __init__(self) -> None:
        self._graph_driver: GraphDriver | None = None
        self._domain_loader: DomainPackLoader | None = None
        self._schedulers: list[Any] = []
        self._compliance_flush_task: asyncio.Task[None] | None = None
        self._gate_reregistration_task: asyncio.Task[None] | None = None
        self._db_pool: Any | None = None

    # --- lifecycle ----------------------------------------------------------

    async def startup(self) -> None:
        from engine.gds.scheduler import GDSScheduler

        logger.info("GraphLifecycle.startup → connecting Neo4j")

        self._graph_driver = GraphDriver(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
        )
        try:
            await self._graph_driver.connect()
            logger.info("GraphLifecycle.startup → Neo4j connected")
        except Exception as exc:
            # Neo4j unreachable at startup — container comes up anyway.
            # Health endpoint will report neo4j=error:connection_failed.
            # The driver retries on first query; no need to crash here.
            logger.warning("GraphLifecycle.startup → Neo4j unavailable: %s", exc)

        self._domain_loader = DomainPackLoader(
            config_path=str(settings.domains_root),
        )

        # W4-04: Optional Postgres pool for ComplianceEngine audit-flush persistence.
        # Soft dependency — None (disabled flush) if DSN unset or Postgres unreachable.
        if settings.postgres_dsn:
            try:
                import asyncpg

                self._db_pool = await asyncpg.create_pool(dsn=settings.postgres_dsn, min_size=1, max_size=5)
                logger.info("GraphLifecycle.startup → Postgres audit pool connected")
            except Exception as exc:
                logger.warning("GraphLifecycle.startup → Postgres audit pool unavailable: %s", exc)
                self._db_pool = None
        else:
            logger.info("POSTGRES_DSN not set — compliance audit flush disabled")

        init_dependencies(self._graph_driver, self._domain_loader, db_pool=self._db_pool)

        # SDK participation is the default (L9-PARTICIPATION-01). The CEG loop
        # runs only when an operator turns that flag off.
        if not settings.sdk_participation_enabled:
            from engine.gate_registration import register_node_with_gate, reregister_with_gate_forever

            await register_node_with_gate()
            if settings.gate_reregistration_enabled and settings.gate_reregistration_interval_seconds > 0:
                self._gate_reregistration_task = asyncio.create_task(
                    reregister_with_gate_forever(settings.gate_reregistration_interval_seconds)
                )

        # Start GDS schedulers for all loaded domains (if GDS enabled).
        # RU-007: with gds_shared_scheduler_registry each started scheduler is
        # registered in EngineState.gds_schedulers (init_dependencies ran above),
        # the one registry admin gds_* reuses and EngineState.shutdown stops.
        if settings.gds_enabled:
            for domain_id in self._domain_loader.list_domains():
                try:
                    spec = self._domain_loader.load_domain(domain_id)
                    if spec.gdsjobs and self._start_domain_scheduler(domain_id, spec, GDSScheduler):
                        logger.info(
                            "GDS scheduler started for domain=%s (%d jobs)",
                            domain_id,
                            len(spec.gdsjobs),
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to start GDS scheduler for domain=%s: %s",
                        domain_id,
                        exc,
                    )
        else:
            logger.info("GDS disabled via settings.gds_enabled=False — skipping scheduler startup")

        # W4-04: Start periodic compliance audit flush task
        self._compliance_flush_task = asyncio.create_task(self._compliance_flush_loop())
        logger.info(
            "W4-04: Compliance flush task started (interval=%ds, buffer_max=%d)",
            settings.compliance_flush_interval,
            settings.compliance_buffer_max,
        )

        # W6-03: Register GDS health probe
        if settings.gds_enabled and self._registered_schedulers():
            self._register_gds_health_probe()

        logger.info("GraphLifecycle.startup complete")

    def _start_domain_scheduler(self, domain_id: str, spec: Any, scheduler_cls: Any) -> bool:
        """Start one domain's GDS scheduler and record it; False if already registered.

        RU-007: with gds_shared_scheduler_registry the scheduler goes into
        EngineState.gds_schedulers (one per domain id); otherwise the boot-local list.
        """
        shared_registry = settings.gds_shared_scheduler_registry
        if shared_registry and domain_id in get_state().gds_schedulers:
            return False
        scheduler = scheduler_cls(spec, self._graph_driver)
        scheduler.register_jobs()
        scheduler.start()
        if shared_registry:
            get_state().gds_schedulers[domain_id] = scheduler
        else:
            self._schedulers.append(scheduler)
        return True

    def _registered_schedulers(self) -> list[Any]:
        """Schedulers the W6-03 probe inspects.

        RU-007: the EngineState registry when gds_shared_scheduler_registry is on;
        the boot-local list otherwise. Read at probe time, not captured.
        """
        if settings.gds_shared_scheduler_registry:
            return list(get_state().gds_schedulers.values())
        return list(self._schedulers)

    def _register_gds_health_probe(self) -> None:
        """W6-03: Register a health probe that checks GDS algorithm staleness."""

        async def gds_health_check() -> dict[str, Any]:
            from datetime import UTC, datetime

            from engine.gds.staleness import StalenessPolicy

            policy = StalenessPolicy.from_settings()
            now = datetime.now(tz=UTC)
            if settings.gds_health_last_attempt_enabled:
                return await self._gds_health_by_last_attempt(policy, now)
            return await self._gds_health_by_newest_entry(policy, now)

        self._gds_health_check = gds_health_check
        logger.info("W6-03: GDS health probe registered")

    @staticmethod
    def _history_key(entry: dict[str, Any], per_schedule: bool) -> Any:
        """GDS-STALE-01: job name per schedule; off keeps the legacy algorithm key."""
        if per_schedule:
            return entry.get("job") or entry.get("algorithm", "unknown")
        return entry.get("algorithm") or entry.get("job", "unknown")

    async def _gds_health_by_newest_entry(self, policy: Any, now: Any) -> dict[str, Any]:
        """W6-03 legacy verdict (gds_health_last_attempt_enabled off): newest entry per key."""
        from engine.gds.staleness import job_staleness, parse_history_timestamp

        stale_jobs: list[str] = []
        for scheduler in self._registered_schedulers():
            history = await scheduler.get_job_history()
            jobs = {job.name: job for job in scheduler.domain_spec.gdsjobs}
            # Most recent entry per job. GDS-STALE-01 keys by job name, so two
            # jobs sharing an algorithm are judged separately; off keeps the
            # legacy algorithm key.
            seen: set[str] = set()
            for entry in reversed(history):
                key = self._history_key(entry, policy.per_schedule)
                if key in seen:
                    continue
                seen.add(key)
                verdict = job_staleness(
                    jobs.get(entry.get("job", "")),
                    parse_history_timestamp(entry.get("timestamp")),
                    now,
                    policy,
                )
                if verdict.stale:
                    stale_jobs.append(key)
        if stale_jobs:
            return {"gds": f"degraded: stale jobs: {', '.join(stale_jobs)}"}
        return {"gds": "ok"}

    async def _gds_health_by_last_attempt(self, policy: Any, now: Any) -> dict[str, Any]:
        """CEG-NATIVE-CLOSURE-001 F07: every declared job, judged by assess_gds_health.

        A job with no successful run (never run after boot included) or whose
        latest success is past its deadline is stale; a job whose success is
        fresh but whose latest attempt is not ``success`` is unhealthy.
        """
        from engine.gds.staleness import assess_gds_health

        stale_jobs: list[str] = []
        unhealthy: list[str] = []
        for scheduler in self._registered_schedulers():
            history = await scheduler.get_job_history()
            jobs = list(scheduler.domain_spec.gdsjobs)
            verdicts = assess_gds_health(jobs, history, now, policy)
            self._classify_gds_verdicts(jobs, verdicts, policy, stale_jobs, unhealthy)
        problems = []
        if stale_jobs:
            problems.append(f"stale jobs: {', '.join(stale_jobs)}")
        if unhealthy:
            problems.append(f"unhealthy jobs: {', '.join(unhealthy)}")
        return {"gds": f"degraded: {'; '.join(problems)}"} if problems else {"gds": "ok"}

    @staticmethod
    def _classify_gds_verdicts(
        jobs: list[Any],
        verdicts: dict[str, Any],
        policy: Any,
        stale_jobs: list[str],
        unhealthy: list[str],
    ) -> None:
        """Append each stale label (once) and each unhealthy ``label (status)``."""
        # GDS-STALE-01 off keeps the legacy algorithm key in the message.
        algorithm = {job.name: job.algorithm for job in jobs}
        for name, verdict in verdicts.items():
            label = name if policy.per_schedule else algorithm.get(name, name)
            if verdict.stale:
                if label not in stale_jobs:
                    stale_jobs.append(label)
            elif not verdict.healthy:
                unhealthy.append(f"{label} ({verdict.last_attempt_status})")

    async def shutdown(self) -> None:
        logger.info("GraphLifecycle.shutdown → stopping schedulers and closing Neo4j pool")

        if self._gate_reregistration_task is not None:
            self._gate_reregistration_task.cancel()
            await asyncio.gather(self._gate_reregistration_task, return_exceptions=True)
            self._gate_reregistration_task = None

        # W4-04: Cancel compliance flush task
        if self._compliance_flush_task is not None:
            self._compliance_flush_task.cancel()
            if settings.boot_cancellation_propagates:
                # gather() absorbs the cancelled task's CancelledError, so shutdown
                # continues; a cancellation of shutdown itself still propagates.
                await asyncio.gather(self._compliance_flush_task, return_exceptions=True)
            else:
                try:
                    await self._compliance_flush_task
                except asyncio.CancelledError:
                    # Previous behavior: swallowed, so shutdown continues even
                    # when shutdown itself was the task being cancelled.
                    pass
            self._compliance_flush_task = None
            logger.info("W4-04: Compliance flush task stopped")

        # Final flush of all compliance engines before shutdown
        state = get_state()
        for domain_id, ce in state.compliance_engines.items():
            try:
                await ce.flush_audit()
            except Exception as exc:
                logger.warning("Final compliance flush failed for %s: %s", domain_id, exc)

        # W4-04: state.shutdown() closes the shared Postgres audit pool (state._db_pool,
        # same object as self._db_pool below) — do not close it a second time here.
        await state.shutdown()
        self._db_pool = None

        logger.info("GraphLifecycle.shutdown complete")

    # --- W4-04: compliance flush loop ----------------------------------------

    async def _compliance_flush_loop(self) -> None:
        """Periodically flush audit entries from all compliance engine singletons."""
        interval = settings.compliance_flush_interval
        while True:
            try:
                await asyncio.sleep(interval)
                state = get_state()
                for domain_id, ce in list(state.compliance_engines.items()):
                    try:
                        await ce.flush_audit()
                    except Exception as exc:
                        logger.warning("Compliance flush failed for domain=%s: %s", domain_id, exc)
            except asyncio.CancelledError:
                if settings.boot_cancellation_propagates:
                    # Let the task end as cancelled; shutdown() awaits it via gather().
                    raise
                break
            except Exception as exc:
                logger.warning("Compliance flush loop error: %s", exc)

    # --- action routing -----------------------------------------------------

    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        tenant: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """Delegate to chassis.actions.execute_action (PacketEnvelope bridge)."""
        from chassis.actions import execute_action

        return await execute_action(
            action=action,
            payload=payload,
            tenant=tenant,
            trace_id=trace_id,
        )
