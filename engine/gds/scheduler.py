"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [gds, scheduler, louvain]
owner: engine-team
status: active
--- /L9_META ---

engine/gds/scheduler.py
APScheduler-based background job execution for graph algorithms.
Manages Louvain community detection, co-occurrence, reinforcement
learning edge weights, and temporal recency pruning.

All algorithms execute real Cypher against Neo4j — no stubs.

Domain neutrality (Stage 2, R01): every node label, relationship type and
property name an algorithm touches comes from the job spec (``projection``,
``sourceedge``, ``writeedge``, ``writeto``, ``writeproperties``) or the domain
ontology. There is no built-in vocabulary. A job that needs a value its spec
does not declare fails explicitly with a ``ValueError`` naming the job and the
missing key; it never falls back to a built-in recipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from engine.config.schema import AggregationStrategy, DomainSpec, EdgeCategory, GDSJobSpec
from engine.config.structural_vocabulary import declared
from engine.gds.cron import crontab_trigger
from engine.gds.node_similarity import (
    NODE_SIMILARITY_ALGORITHM,
    NODE_SIMILARITY_ALGORITHMS,
    build_node_similarity_plan,
)
from engine.gds.staleness import next_fire_after
from engine.graph.driver import GraphDriver
from engine.utils.security import cypher_number, sanitize_label

# S2-10: EdgeCategory → recommended GDS configuration mapping.
# Paper: different relation types benefit from different algorithms/aggregation.
_EDGE_CATEGORY_DEFAULTS: dict[str, dict[str, str]] = {
    EdgeCategory.TRANSACTION: {"algorithm": "reinforcement", "aggregation": "attention_weighted"},
    EdgeCategory.CAPABILITY: {"algorithm": "louvain", "aggregation": "mean"},
    EdgeCategory.TAXONOMY: {"algorithm": "louvain", "aggregation": "mean"},
    EdgeCategory.MARKET: {"algorithm": "cooccurrence", "aggregation": "attention_weighted"},
    EdgeCategory.CONTEXT: {"algorithm": "temporalrecency", "aggregation": "sample_aggregate"},
    EdgeCategory.REFERRAL: {"algorithm": "reinforcement", "aggregation": "attention_weighted"},
    EdgeCategory.INTENT: {"algorithm": "cooccurrence", "aggregation": "attention_weighted"},
    EdgeCategory.PRECOMPUTED: {"algorithm": "louvain", "aggregation": "mean"},
    EdgeCategory.EXCLUSION: {"algorithm": "louvain", "aggregation": "mean"},
}

logger = logging.getLogger(__name__)

_MAX_HISTORY_SIZE = 1000

# Algorithms execute_job dispatches. A job naming anything else has nothing to run.
_KNOWN_ALGORITHMS: frozenset[str] = frozenset(
    {
        "louvain",
        "cooccurrence",
        "reinforcement",
        "temporalrecency",
        "geoproximity",
        "equipmentsync",
        "feedback_recalculation",
        "causal_chain_scoring",
        *NODE_SIMILARITY_ALGORITHMS,
    }
)


class GDSScheduler:
    """
    Schedules and executes GDS jobs defined in domain spec YAML.

    Supported algorithms:
        - louvain: Community detection via GDS Louvain
        - cooccurrence: Bipartite projection → co-occurrence edges
        - reinforcement: Outcome-weighted positive/negative edge scoring over a
          declared interaction edge
        - temporalrecency: Recency-decay overlay edge creation + pruning over a
          declared interaction edge
        - geoproximity: Haversine distance-based proximity edges
        - equipmentsync: Boolean property → edge to a declared type node
          (the algorithm id is historical; the vocabulary is the job spec's)
        - nodeSimilarity (alias node_similarity): GDS node similarity over the job's projection,
          written as ``writeedge`` relationships scored in ``writeproperty``
          (gated by ``gds_node_similarity_enabled``; see node_similarity.py)

    Dependencies (``gds_dependency_barrier_enabled``, CEG-NATIVE-CLOSURE-001
    F04): ``depends_on`` names algorithms; every declared job running one of
    them is a provider. A dependent job — fired by cron or ``trigger_job`` —
    starts its graph effect only when every provider has no run in flight, no
    cron fire due since its last start, and a latest attempt of ``success``.
    While a provider runs or is due the dependent waits, at most
    ``dependency_wait_timeout_sec``; otherwise it records ``blocked`` with the
    unsatisfied providers and writes nothing. A dependency on an undeclared
    algorithm, a dependency cycle, and any job downstream of either are
    ``refused``: never cron-registered, and refused on manual trigger.
    Provider state is this scheduler's own (it starts empty, like the job
    history), so after a restart a dependent waits for its providers' next
    successful run. A job counts as running from the moment it is triggered,
    including while it waits on its own providers, and once per concurrent
    attempt; a run cancelled after its effect started records ``cancelled``.
    """

    # Upper bound on how long a dependent job waits for a running or due provider.
    dependency_wait_timeout_sec: float = 3600.0

    def __init__(self, domain_spec: DomainSpec, graph_driver: GraphDriver):
        self.domain_spec = domain_spec
        self.graph_driver = graph_driver
        self.scheduler = AsyncIOScheduler()
        self._job_history: deque[dict[str, Any]] = deque(maxlen=_MAX_HISTORY_SIZE)
        self._history_lock = asyncio.Lock()
        # F04 run-time barrier state, keyed by job name.
        self._state_changed = asyncio.Condition()
        self._in_flight: Counter[str] = Counter()
        self._last_started: dict[str, datetime] = {}
        self._last_attempt_status: dict[str, str] = {}
        self._started_at: datetime | None = None
        # Attempts are numbered at entry; only the latest-started attempt of a
        # job sets its status, so an older attempt finishing last cannot hide a
        # newer one's failure.
        self._attempt_seq = 0
        self._status_seq: dict[str, int] = {}
        self._refused_jobs: dict[str, str] | None = None

    @property
    def refused_jobs(self) -> dict[str, str]:
        """F04: declared jobs that can never execute, by name, with the reason (computed once)."""
        if self._refused_jobs is None:
            self._refused_jobs = self.dependency_refusals(list(self.domain_spec.gdsjobs))
        return self._refused_jobs

    @staticmethod
    def _get_job_parameter(job_spec: GDSJobSpec, param_name: str, default: float) -> float:
        """Extract a numeric parameter from job_spec.writeproperties."""
        if not job_spec.writeproperties:
            return default
        for prop in job_spec.writeproperties:
            if param_name in prop:
                try:
                    return float(prop[param_name])
                except (ValueError, TypeError):
                    logger.warning(f"Invalid {param_name} in job {job_spec.name}, using default")
                    return default
        return default

    @staticmethod
    def _get_job_setting(job_spec: GDSJobSpec, key: str) -> Any | None:
        """Return a named entry from ``job_spec.writeproperties``, or None."""
        for prop in job_spec.writeproperties or []:
            if key in prop:
                return prop[key]
        return None

    @staticmethod
    def _declared_job_setting(job_spec: GDSJobSpec, key: str, kind: str) -> str:
        """A structural ``writeproperties`` entry the domain must declare (Stage 2 follow-up, item 8)."""
        return declared(
            GDSScheduler._get_job_setting(job_spec, key),
            kind,
            f"GDS job {job_spec.name!r} ({job_spec.algorithm}) declares no writeproperties entry {key!r}",
        )

    @staticmethod
    def _require_job_setting(job_spec: GDSJobSpec, key: str) -> Any:
        """Return a named ``writeproperties`` entry or fail explicitly.

        The domain owns the vocabulary an algorithm runs over. When a job does
        not declare a value the algorithm needs, that is a configuration error
        the pack author must fix — not a cue to guess.
        """
        value = GDSScheduler._get_job_setting(job_spec, key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                f"Job {job_spec.name}: algorithm {job_spec.algorithm!r} requires writeproperties entry {key!r}; "
                "the domain spec must declare it (no built-in default)"
            )
        return value

    @staticmethod
    def _require_edge(job_spec: GDSJobSpec, field: str) -> str:
        """Return a sanitized relationship type from a job-spec field or fail explicitly."""
        value = getattr(job_spec, field, None)
        if not value:
            raise ValueError(
                f"Job {job_spec.name}: algorithm {job_spec.algorithm!r} requires {field!r}; "
                "the domain spec must declare the relationship type (no built-in default)"
            )
        return sanitize_label(str(value))

    def _get_candidate_label(self, job_spec: GDSJobSpec) -> str:
        """
        Get the node label a job operates over.

        Resolution order: the job's own ``projection.nodelabels``, then the
        domain's declared candidate match entity, then an ontology node marked
        ``candidate``. There is no further fallback: a domain that declares none
        of these has not told the engine what to compute over, and the job fails
        explicitly rather than running against a label the engine invented.
        """
        if job_spec.projection and job_spec.projection.nodelabels:
            return sanitize_label(job_spec.projection.nodelabels[0])

        if self.domain_spec.matchentities and self.domain_spec.matchentities.candidate:
            return sanitize_label(self.domain_spec.matchentities.candidate[0].label)

        for node in self.domain_spec.ontology.nodes:
            if getattr(node, "candidate", False):
                return sanitize_label(node.label)

        raise ValueError(
            f"Job {job_spec.name}: no node label declared — set projection.nodelabels on the job "
            "or declare a candidate match entity in the domain spec"
        )

    def resolve_aggregation_strategy(self, job_spec: GDSJobSpec) -> str:
        """S2-03: Auto-select aggregation strategy based on edge category and node role.

        HGKR paper (p.6): GAT aggregator for bipartite graphs tailed with
        recommendation item type; GraphSAGE for the rest.

        Rule: Edges terminating at candidate nodes → attention_weighted
              Edges terminating at taxonomy/auxiliary nodes → mean
        """
        if job_spec.aggregation_strategy != AggregationStrategy.AUTO:
            return job_spec.aggregation_strategy.value

        # Check if any target node in the projection is a candidate
        for label in job_spec.projection.nodelabels:
            node_spec = self._find_node_spec(label)
            if node_spec and node_spec.candidate:
                return AggregationStrategy.ATTENTION_WEIGHTED.value

        return AggregationStrategy.MEAN.value

    def _find_node_spec(self, label: str) -> Any:
        """Find NodeSpec by label in domain ontology."""
        for node in self.domain_spec.ontology.nodes:
            if node.label == label:
                return node
        return None

    def register_jobs(self) -> None:
        """Register all GDS jobs from domain spec using DAG ordering.

        Jobs are topologically sorted by depends_on relationships.
        Jobs without depends_on are placed in wave 0 (backward compatible).
        """
        from engine.config.settings import settings

        barrier = settings.gds_dependency_barrier_enabled
        waves = self._build_execution_dag(self.domain_spec.gdsjobs)
        registered = 0
        for wave_idx, wave in enumerate(waves):
            for job_spec in wave:
                refusal = self._refusal(job_spec) if barrier else None
                if refusal is not None:
                    # F04: no fallback wave — an unexecutable job is never scheduled.
                    logger.error("GDS job %s refused, not scheduled: %s", job_spec.name, refusal)
                    continue
                if job_spec.schedule.type == "cron":
                    self._register_cron_job(job_spec)
                    registered += 1
                elif job_spec.schedule.type == "manual":
                    logger.info("Job %s (wave %d) is manual-trigger only", job_spec.name, wave_idx)
        # RU-096: report what was registered, not how many jobs the spec declares.
        logger.info(
            "Registered %d GDS cron jobs (of %d declared) across %d execution waves",
            registered,
            len(self.domain_spec.gdsjobs),
            len(waves),
        )

    @staticmethod
    def _build_execution_dag(jobs: list[GDSJobSpec]) -> list[list[GDSJobSpec]]:
        """Build execution waves via Kahn's topological sort algorithm.

        Args:
            jobs: List of GDS job specs with optional depends_on fields.

        Returns:
            List of waves, where each wave is a list of jobs that can
            execute in parallel. Wave N completes before wave N+1 starts.

        Backward compatibility:
            Jobs without depends_on are placed in wave 0 (parallel execution),
            matching pre-enhancement behavior exactly.

        Cycle handling:
            Circular dependencies are detected and logged as errors.
            Cycle members are placed in a fallback wave at the end.
        """
        if not jobs:
            return []

        from engine.config.settings import settings

        # The dependency barrier needs every provider job registered, so it
        # implies the name-keyed DAG (an algorithm key collapses same-algorithm jobs).
        if settings.gds_dag_job_identity_by_name or settings.gds_dependency_barrier_enabled:
            return GDSScheduler._build_execution_dag_by_job(jobs)

        job_map: dict[str, GDSJobSpec] = {j.algorithm: j for j in jobs}
        in_degree: dict[str, int] = {j.algorithm: 0 for j in jobs}
        dependents: dict[str, list[str]] = defaultdict(list)

        for job in jobs:
            deps = job.depends_on if job.depends_on else []
            for dep in deps:
                if dep in job_map:
                    in_degree[job.algorithm] += 1
                    dependents[dep].append(job.algorithm)
                else:
                    logger.warning(
                        "Job '%s' depends on unknown algorithm '%s'; ignoring dependency",
                        job.name,
                        dep,
                    )

        # Kahn's algorithm: process nodes with in-degree 0 in waves
        waves: list[list[GDSJobSpec]] = []
        queue = [alg for alg, deg in in_degree.items() if deg == 0]

        while queue:
            waves.append([job_map[alg] for alg in queue])
            next_queue: list[str] = []
            for alg in queue:
                for dep_alg in dependents[alg]:
                    in_degree[dep_alg] -= 1
                    if in_degree[dep_alg] == 0:
                        next_queue.append(dep_alg)
            queue = next_queue

        # Detect cycles: any remaining nodes with in_degree > 0
        placed = {alg for wave in waves for alg in [j.algorithm for j in wave]}
        all_algs = {j.algorithm for j in jobs}
        cycle_members = all_algs - placed

        if cycle_members:
            logger.error(
                "Circular dependency detected among GDS jobs: %s. Placing in fallback wave.",
                sorted(cycle_members),
            )
            waves.append([job_map[alg] for alg in sorted(cycle_members)])

        return waves

    @staticmethod
    def _build_execution_dag_by_job(jobs: list[GDSJobSpec]) -> list[list[GDSJobSpec]]:
        """RU-096 / CEG-COR-036: Kahn's sort with jobs (keyed by name) as nodes.

        Keying by algorithm collapsed every same-algorithm job into the last
        one declared, so only one of two ``louvain`` cron jobs was scheduled.
        ``depends_on`` keeps its schema meaning — algorithm names — and resolves
        to every job whose algorithm matches. Waves preserve declaration order;
        an unknown dependency is ignored with a warning and cycle members go to
        a final fallback wave, exactly as before.
        """
        job_map: dict[str, GDSJobSpec] = {}
        for job in jobs:
            job_map[job.name] = job
        names = list(job_map)
        by_algorithm: dict[str, list[str]] = defaultdict(list)
        for name in names:
            by_algorithm[job_map[name].algorithm].append(name)

        in_degree: dict[str, int] = dict.fromkeys(names, 0)
        dependents: dict[str, list[str]] = defaultdict(list)
        for name in names:
            job = job_map[name]
            for dep in job.depends_on or []:
                providers = by_algorithm.get(dep)
                if not providers:
                    logger.warning(
                        "Job '%s' depends on unknown algorithm '%s'; ignoring dependency",
                        job.name,
                        dep,
                    )
                    continue
                for provider in providers:
                    in_degree[name] += 1
                    dependents[provider].append(name)

        order = {name: idx for idx, name in enumerate(names)}
        waves: list[list[GDSJobSpec]] = []
        queue = [name for name in names if in_degree[name] == 0]
        while queue:
            waves.append([job_map[name] for name in queue])
            next_queue: list[str] = []
            for name in queue:
                for dependent in dependents[name]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_queue.append(dependent)
            queue = sorted(next_queue, key=order.__getitem__)

        placed = {job.name for wave in waves for job in wave}
        cycle_members = [name for name in names if name not in placed]
        if cycle_members:
            logger.error(
                "Circular dependency detected among GDS jobs: %s. Placing in fallback wave.",
                sorted(cycle_members),
            )
            waves.append([job_map[name] for name in sorted(cycle_members)])

        return waves

    @staticmethod
    def dependency_refusals(jobs: list[GDSJobSpec]) -> dict[str, str]:
        """F04: jobs whose ``depends_on`` can never be satisfied, with the reason.

        A job name declared more than once (provider state is keyed by name),
        a dependency on an algorithm no declared job runs, membership of a
        dependency cycle (including a job depending on its own algorithm), and
        depending on a job refused for any of these reasons. Keyed by job name; an
        independent job is never listed.
        """
        by_algorithm: dict[str, list[str]] = defaultdict(list)
        for job in jobs:
            by_algorithm[job.algorithm].append(job.name)

        refused: dict[str, str] = {}
        # Provider state is keyed by job name: a name declared twice is ambiguous.
        declared_names = Counter(job.name for job in jobs)
        for name, count in declared_names.items():
            if count > 1:
                refused[name] = f"job name declared {count} times; dependency state needs a unique name"
        providers: dict[str, list[str]] = {}
        for job in jobs:
            names: list[str] = []
            for dep in job.depends_on or []:
                if dep not in by_algorithm:
                    refused.setdefault(job.name, f"depends on undeclared algorithm {dep!r}")
                names.extend(by_algorithm.get(dep, []))
            providers[job.name] = names

        # Kahn over the provider graph: whatever never reaches in-degree 0 is in,
        # or downstream of, a cycle.
        in_degree = {name: len(set(deps)) for name, deps in providers.items()}
        dependents: dict[str, list[str]] = defaultdict(list)
        for name, deps in providers.items():
            for provider in set(deps):
                dependents[provider].append(name)
        queue = [name for name, degree in in_degree.items() if degree == 0]
        placed: set[str] = set()
        while queue:
            name = queue.pop()
            placed.add(name)
            for dependent in dependents[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        unplaced = [name for name in providers if name not in placed]

        # A cycle member is an unplaced job reachable from itself.
        def _reaches(start: str, target: str) -> bool:
            seen: set[str] = set()
            stack = list(dependents[start])
            while stack:
                node = stack.pop()
                if node == target:
                    return True
                if node not in seen:
                    seen.add(node)
                    stack.extend(dependents[node])
            return False

        cycle = sorted(name for name in unplaced if _reaches(name, name))
        for name in cycle:
            refused.setdefault(name, f"dependency cycle among jobs {cycle}")

        changed = True
        while changed:
            changed = False
            for name, deps in providers.items():
                if name in refused:
                    continue
                blocker = next((p for p in deps if p in refused), None)
                if blocker is not None:
                    refused[name] = f"depends on unexecutable job {blocker!r}"
                    changed = True
        return refused

    def _register_cron_job(self, job_spec: GDSJobSpec) -> None:
        if not job_spec.schedule.cron:
            raise ValueError(f"Job {job_spec.name}: cron expression required")
        trigger = crontab_trigger(job_spec.schedule.cron)
        self.scheduler.add_job(
            func=self.execute_job,
            trigger=trigger,
            args=[job_spec],
            id=job_spec.name,
            name=job_spec.name,
            replace_existing=True,
        )
        logger.info(f"Scheduled job {job_spec.name} with cron {job_spec.schedule.cron}")

    async def execute_job(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """Execute GDS job and record result.

        S2-03: Resolves aggregation strategy before execution.
        S2-15: Supports stability_runs for non-deterministic algorithms.
        """
        resolved_agg = self.resolve_aggregation_strategy(job_spec)
        logger.info(f"Starting GDS job {job_spec.name} — algorithm={job_spec.algorithm}, aggregation={resolved_agg}")
        from engine.config.settings import settings

        # RU-100 / CEG-COR-086: start, duration and the history timestamp share
        # one clock — timezone-aware UTC unless the flag restores naive local.
        utc = settings.gds_history_utc_timestamps

        def _now() -> datetime:
            return datetime.now(tz=UTC) if utc else datetime.now()

        barrier = settings.gds_dependency_barrier_enabled
        name = job_spec.name
        # In flight from entry, while waiting on its own providers too, so a job
        # depending on this one never reads an older success (one count per attempt).
        self._in_flight[name] += 1
        self._attempt_seq += 1
        seq = self._attempt_seq
        effect_started = False
        try:
            if barrier:
                refusal = self._refusal(job_spec)
                if refusal is not None:
                    logger.error("GDS job %s refused: %s", name, refusal)
                    return await self._record(job_spec, {"status": "refused", "reason": refusal}, _now, seq)
                unsatisfied = await self._await_dependencies(job_spec)
                if unsatisfied:
                    logger.warning("GDS job %s blocked by dependencies: %s", name, unsatisfied)
                    return await self._record(
                        job_spec,
                        {
                            "status": "blocked",
                            "reason": "declared dependencies not satisfied",
                            "unsatisfied": unsatisfied,
                        },
                        _now,
                        seq,
                    )
            self._last_started[name] = datetime.now(tz=UTC)
            effect_started = True
            return await self._execute_effect(job_spec, resolved_agg, _now, seq)
        except asyncio.CancelledError:
            # Every cancelled attempt is in the history as ``cancelled``, so health
            # never reads it as a success.
            self._job_history.append(
                {
                    "job": name,
                    "algorithm": job_spec.algorithm,
                    "timestamp": _now().isoformat(),
                    "status": "cancelled",
                    "effect_started": effect_started,
                }
            )
            if effect_started and seq >= self._status_seq.get(name, 0):
                # The effect may be half-written: the previous success no longer stands.
                # Cancelled before the effect started, nothing was written and the
                # provider's earlier success keeps satisfying its dependents.
                self._status_seq[name] = seq
                self._last_attempt_status[name] = "cancelled"
            raise
        finally:
            self._in_flight[name] -= 1
            if self._in_flight[name] <= 0:
                del self._in_flight[name]
            with contextlib.suppress(asyncio.CancelledError):
                async with self._state_changed:
                    self._state_changed.notify_all()

    async def _execute_effect(self, job_spec: GDSJobSpec, resolved_agg: str, _now: Any, seq: int) -> dict[str, Any]:
        """Run the job's algorithm and record the attempt (status from the algorithm)."""
        start = _now()
        result: dict[str, Any] = {}

        try:
            # S2-15: Multi-run stability averaging
            runs = max(1, getattr(job_spec, "stability_runs", 1))
            if runs > 1 and job_spec.algorithm == "louvain":
                result = await self._run_stable_louvain(job_spec, runs)
            else:
                algo = job_spec.algorithm
                if algo == "louvain":
                    result = await self._run_louvain(job_spec)
                elif algo == "cooccurrence":
                    result = await self._run_cooccurrence(job_spec)
                elif algo == "reinforcement":
                    result = await self._run_reinforcement(job_spec)
                elif algo == "temporalrecency":
                    result = await self._run_temporal_recency(job_spec)
                elif algo == "geoproximity":
                    result = await self._run_geoproximity(job_spec)
                elif algo == "equipmentsync":
                    result = await self._run_equipment_sync(job_spec)
                elif algo == "feedback_recalculation":
                    result = await self._run_feedback_recalculation(job_spec)
                elif algo == "causal_chain_scoring":
                    result = await self._run_causal_chain_scoring(job_spec)
                elif algo in NODE_SIMILARITY_ALGORITHMS:
                    result = await self._run_node_similarity(job_spec)
                else:
                    # Barrier off only: with it on, _refusal stopped this job first.
                    logger.warning(f"Unknown algorithm: {algo}")
                    result = {"status": "skipped", "reason": f"unknown algorithm: {algo}"}

            duration = (_now() - start).total_seconds()
            logger.info(f"Completed job {job_spec.name} in {duration:.2f}s")
            result["duration_sec"] = duration
            result["status"] = result.get("status", "success")
            result["aggregation_strategy"] = resolved_agg

        except Exception as e:
            logger.exception(f"Job {job_spec.name} failed")
            result = {"status": "failed", "error": str(e)}

        return await self._record(job_spec, result, _now, seq)

    async def _record(self, job_spec: GDSJobSpec, result: dict[str, Any], _now: Any, seq: int) -> dict[str, Any]:
        """Append one attempt to the history; the latest-started attempt sets the provider state."""
        async with self._history_lock:
            self._job_history.append(
                {
                    "job": job_spec.name,
                    "algorithm": job_spec.algorithm,
                    "timestamp": _now().isoformat(),
                    **result,
                }
            )
        async with self._state_changed:
            if seq >= self._status_seq.get(job_spec.name, 0):
                self._status_seq[job_spec.name] = seq
                self._last_attempt_status[job_spec.name] = str(result.get("status"))
            self._state_changed.notify_all()
        return result

    def _refusal(self, job_spec: GDSJobSpec) -> str | None:
        """F04: why this job can never execute, or None."""
        if job_spec.name in self.refused_jobs:
            return self.refused_jobs[job_spec.name]
        if job_spec.algorithm not in _KNOWN_ALGORITHMS:
            return f"unknown algorithm: {job_spec.algorithm}"
        return None

    def _providers(self, job_spec: GDSJobSpec) -> list[GDSJobSpec]:
        """Every declared job running an algorithm ``job_spec`` depends on (declaration order)."""
        deps = set(job_spec.depends_on or [])
        return [job for job in self.domain_spec.gdsjobs if job.algorithm in deps and job.name != job_spec.name]

    def _provider_wait(self, provider: GDSJobSpec, now: datetime) -> str | None:
        """Why the dependent must keep waiting on ``provider``, or None."""
        if self._in_flight.get(provider.name):
            return f"{provider.name}: running"
        if provider.schedule.type != "cron" or not self.scheduler.running:
            return None
        if self._refusal(provider) is not None:
            return None  # never cron-registered, so never due: block on its status instead
        reference = self._last_started.get(provider.name, self._started_at)
        if reference is None:
            return None
        fire = next_fire_after(provider.schedule.cron, reference)
        if fire is not None and fire <= now:
            return f"{provider.name}: due"
        return None

    def _provider_unsatisfied(self, provider: GDSJobSpec) -> str | None:
        status = self._last_attempt_status.get(provider.name)
        if status is None:
            return f"{provider.name}: never run"
        if status != "success":
            return f"{provider.name}: last attempt {status}"
        return None

    async def _await_dependencies(self, job_spec: GDSJobSpec) -> list[str]:
        """Wait for running/due providers; return the unsatisfied ones (empty = go)."""
        providers = self._providers(job_spec)
        if not providers:
            return []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.dependency_wait_timeout_sec
        async with self._state_changed:
            while True:
                now = datetime.now(tz=UTC)
                waiting = [w for p in providers if (w := self._provider_wait(p, now)) is not None]
                if not waiting:
                    return [u for p in providers if (u := self._provider_unsatisfied(p)) is not None]
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return waiting
                # Re-check at least every second: a cron fire becomes due without a notification.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._state_changed.wait(), min(remaining, 1.0))

    # ── Algorithm Implementations ──────────────────────────────

    async def _run_louvain(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """Run Louvain community detection via GDS."""
        db = self.domain_spec.domain.id
        # Sanitize job name for use in graph projection name
        safe_job_name = sanitize_label(job_spec.name)
        graph_name = f"{safe_job_name}_graph"

        # Pre-cleanup: drop stale projection if it exists (fixes crash on re-run)
        gds_params = {"graph_name": graph_name}
        pre_drop = """
        CALL gds.graph.exists($graph_name) YIELD exists
        WITH exists WHERE exists
        CALL gds.graph.drop($graph_name) YIELD graphName
        RETURN graphName
        """
        try:
            await self.graph_driver.execute_query(pre_drop, parameters=gds_params, database=db)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "not found" in exc_msg or "does not exist" in exc_msg:
                pass  # Graph didn't exist; expected on first run
            else:
                logger.exception("GDS pre-drop failed unexpectedly for %s", graph_name)
                raise

        # Use json.dumps for proper Cypher array syntax
        node_labels = json.dumps(job_spec.projection.nodelabels)
        edge_types = json.dumps(job_spec.projection.edgetypes)
        # Sanitize write property name
        # Stage 2 follow-up (item 8, M8): the community property Louvain writes is
        # the job's declaration, the same one the community scoring readers declare.
        write_prop = sanitize_label(
            declared(
                job_spec.writeproperty,
                "louvain_write",
                f"GDS job {job_spec.name!r} (louvain) declares no writeproperty",
            )
        )

        gds_params["write_prop"] = write_prop
        project_cypher = f"""
        CALL gds.graph.project($graph_name, {node_labels}, {edge_types})
        YIELD graphName, nodeCount, relationshipCount
        RETURN graphName, nodeCount, relationshipCount
        """
        try:
            await self.graph_driver.execute_query(project_cypher, parameters=gds_params, database=db)

            louvain_cypher = """
            CALL gds.louvain.write($graph_name, {writeProperty: $write_prop})
            YIELD communityCount, modularity
            RETURN communityCount, modularity
            """
            result = await self.graph_driver.execute_query(louvain_cypher, parameters=gds_params, database=db)
            data = result[0] if result else {}
            logger.info(f"Louvain: {data}")
            return {"communities": data.get("communityCount"), "modularity": data.get("modularity")}
        finally:
            drop_cypher = "CALL gds.graph.drop($graph_name) YIELD graphName RETURN graphName"
            try:
                await self.graph_driver.execute_query(drop_cypher, parameters=gds_params, database=db)
            except Exception:
                logger.exception(f"Failed to drop projected graph '{graph_name}'")

    async def _run_node_similarity(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """CEG-GDS-SIM: run ``gds.nodeSimilarity.write`` over the job's projection.

        Flag off → skipped with an explicit reason and no database call. On:
        drop any stale projection, clear this job's leftover staging edges,
        project, write into the job's staging type, then swap: one statement
        deletes the edges this job wrote last time (ownership marker) and
        promotes the staged ones. The previous result survives any failure
        before the swap. The projection is always dropped. All statements
        target the tenant's domain database.
        """
        from engine.config.settings import settings

        if not settings.gds_node_similarity_enabled:
            return {
                "status": "skipped",
                "reason": "gds_node_similarity_enabled is off; nodeSimilarity job not run",
            }

        plan = build_node_similarity_plan(job_spec)
        db = self.domain_spec.domain.id
        try:
            await self.graph_driver.execute_query(plan.pre_drop_cypher, parameters=plan.params, database=db)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "not found" not in exc_msg and "does not exist" not in exc_msg:
                logger.exception("GDS pre-drop failed unexpectedly for %s", plan.graph_name)
                raise

        await self.graph_driver.execute_query(plan.clear_staging_cypher, parameters=plan.params, database=db)
        try:
            # Inside the cleanup scope: a projection the server registered
            # before the call failed is still dropped.
            await self.graph_driver.execute_query(plan.project_cypher, parameters=plan.params, database=db)
            written = await self.graph_driver.execute_query(plan.write_cypher, parameters=plan.params, database=db)
            data = written[0] if written else {}
            swapped = await self.graph_driver.execute_query(plan.swap_cypher, parameters=plan.params, database=db)
            swap = swapped[0] if swapped else {}
        finally:
            try:
                await self.graph_driver.execute_query(plan.drop_cypher, parameters=plan.params, database=db)
            except Exception:
                logger.exception("Failed to drop projected graph '%s'", plan.graph_name)

        deleted = swap.get("relationships_deleted", 0)
        logger.info(
            "nodeSimilarity %s: %s nodes compared, %s relationships written, %s replaced",
            job_spec.name,
            data.get("nodesCompared"),
            data.get("relationshipsWritten"),
            deleted,
        )
        return {
            "algorithm": NODE_SIMILARITY_ALGORITHM,
            "nodes_compared": data.get("nodesCompared"),
            "relationships_written": swap.get("relationships_written", 0),
            "relationships_deleted": deleted,
            "write_edge": plan.write_edge,
            "write_property": plan.write_property,
            "top_k": plan.config["topK"],
        }

    async def _run_cooccurrence(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """Build co-occurrence edges from bipartite projection.

        Applies OmniSage power-law pruning: for high-degree nodes (>10 edges),
        retain only d^0.86 edges to prevent hub dominance in community detection.
        """
        if not job_spec.sourceedge or not job_spec.writeedge:
            raise ValueError(f"Job {job_spec.name}: sourceedge and writeedge required")
        source_edge = sanitize_label(job_spec.sourceedge)
        write_edge = sanitize_label(job_spec.writeedge)
        db = self.domain_spec.domain.id
        pruning_alpha = 0.86

        # Step 1: Build co-occurrence edges
        cypher = f"""
        MATCH (a)-[:{source_edge}]->(common)<-[:{source_edge}]-(b)
        WHERE id(a) < id(b)
        WITH a, b, count(common) AS weight
        WHERE weight >= 2
        MERGE (a)-[r:{write_edge}]->(b)
        SET r.weight = weight, r.updated_at = datetime()
        RETURN count(r) AS edges_created
        """
        result = await self.graph_driver.execute_query(cypher, database=db)
        edges = result[0]["edges_created"] if result else 0

        # Step 2: Power-law pruning — prune high-degree nodes to d^0.86 edges
        # Only prune nodes with >10 co-occurrence edges to avoid over-pruning
        from engine.config.settings import settings

        if settings.gds_cooccurrence_deterministic_prune:
            prune_cypher = self._deterministic_cooccurrence_prune_cypher(write_edge, pruning_alpha)
        else:
            prune_cypher = self._legacy_cooccurrence_prune_cypher(write_edge, pruning_alpha)
        try:
            prune_result = await self.graph_driver.execute_query(prune_cypher, database=db)
            pruned = prune_result[0]["pruned_edges"] if prune_result else 0
        except Exception as e:
            if settings.gds_prune_failure_partial_enabled:
                # F05: the edges are written but the declared bound is not applied.
                logger.exception("Co-occurrence prune failed after %s edges were written", edges)
                return {
                    "status": "partial",
                    "edges_created": edges,
                    "edges_pruned": None,
                    "pruning_alpha": pruning_alpha,
                    "prune_error": str(e),
                }
            logger.warning(f"Power-law pruning skipped: {e}")
            pruned = 0

        logger.info(f"Co-occurrence: {edges} edges created/updated, {pruned} pruned (alpha={pruning_alpha})")
        return {"edges_created": edges, "edges_pruned": pruned, "pruning_alpha": pruning_alpha}

    @staticmethod
    def _deterministic_cooccurrence_prune_cypher(write_edge: str, pruning_alpha: float) -> str:
        """RU-098 / CEG-COR-168: weight-ordered, deterministic power-law pruning.

        Every node with degree > 10 keeps its strongest
        ``keep_count = floor(degree ^ alpha)`` edges, ordered by ``r.weight``
        DESC with ``elementId(r)`` as the tie-break. An edge is deleted only
        when *both* endpoints prune and neither keeps it (two rejection votes),
        so a node never ends below its keep count and a node with degree <= 10
        keeps all of its edges. Identical input gives an identical result.
        ``write_edge`` arrives sanitized and is re-validated here.
        """
        edge = sanitize_label(write_edge)
        alpha = cypher_number(pruning_alpha)
        return f"""
        MATCH (n)-[r:{edge}]-()
        WITH n, r
        ORDER BY r.weight DESC, elementId(r)
        WITH n, collect(r) AS edges
        WITH n, edges, size(edges) AS degree
        WHERE degree > 10
        WITH n, edges[toInteger(degree ^ {alpha})..] AS rejected
        UNWIND rejected AS r
        WITH r, count(*) AS votes
        WHERE votes = 2
        DELETE r
        RETURN count(r) AS pruned_edges
        """

    @staticmethod
    def _legacy_cooccurrence_prune_cypher(write_edge: str, pruning_alpha: float) -> str:
        """Pre-RU-098 prune: unordered collect, per-endpoint deletion."""
        edge = sanitize_label(write_edge)
        alpha = cypher_number(pruning_alpha)
        return f"""
        MATCH (n)-[r:{edge}]-()
        WITH n, collect(r) AS edges, count(r) AS degree
        WHERE degree > 10
        WITH n, edges, degree, toInteger(degree ^ {alpha}) AS keep_count
        WITH n, edges[keep_count..] AS to_prune
        UNWIND to_prune AS r
        DELETE r
        RETURN count(r) AS pruned_edges
        """

    async def _run_reinforcement(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """
        Weight edges by outcome feedback recorded on a declared interaction edge.

        Aggregates per (source, target) pair over ``sourceedge`` relationships:
        - a positive edge (``writeedge``) where at least one interaction carried
          ``success_value`` in ``outcome_property``;
        - a negative edge (``negative_writeedge``) where at least one carried
          ``failure_value``.

        Each edge gets ``count``, ``success_rate`` (positive edge only),
        ``last_event_date`` and ``recency_score = exp(-age_days / half_life)``.
        Optional aggregates are written only when the job names the property:
        ``value_property`` → ``aggregate_value``, ``quality_property`` →
        ``avg_quality``, ``reason_property`` → ``primary_reason``.

        Required job spec: ``sourceedge``, ``writeedge``, and the
        ``writeproperties`` entry ``negative_writeedge``. Optional entries with
        generic defaults: ``outcome_property`` (``outcome``), ``success_value``
        (``success``), ``failure_value`` (``failure``), ``timestamp_property``
        (``created_at``), ``positive_half_life_days`` (180), ``negative_half_life_days``
        (90), ``prune_below`` (0.1).
        """
        db = self.domain_spec.domain.id
        node_label = self._get_candidate_label(job_spec)
        source_edge = self._require_edge(job_spec, "sourceedge")
        positive_edge = self._require_edge(job_spec, "writeedge")
        negative_edge = sanitize_label(str(self._require_job_setting(job_spec, "negative_writeedge")))
        outcome_prop = sanitize_label(str(self._get_job_setting(job_spec, "outcome_property") or "outcome"))
        ts_prop = sanitize_label(str(self._get_job_setting(job_spec, "timestamp_property") or "created_at"))
        positive_half_life = self._get_job_parameter(job_spec, "positive_half_life_days", 180.0)
        negative_half_life = self._get_job_parameter(job_spec, "negative_half_life_days", 90.0)
        prune_below = self._get_job_parameter(job_spec, "prune_below", 0.1)

        # Outcome values are data and travel as $parameters, never as literals (C-009).
        params: dict[str, Any] = {
            "success_value": self._get_job_setting(job_spec, "success_value") or "success",
            "failure_value": self._get_job_setting(job_spec, "failure_value") or "failure",
            "positive_half_life": positive_half_life,
            "negative_half_life": negative_half_life,
            "prune_below": prune_below,
        }

        optional_sets: list[str] = []
        optional_with: list[str] = []
        value_prop = self._get_job_setting(job_spec, "value_property")
        if value_prop:
            safe = sanitize_label(str(value_prop))
            optional_with.append(
                f"sum(CASE WHEN t.{outcome_prop} = $success_value THEN coalesce(t.{safe}, 0) ELSE 0 END) AS total_value"
            )
            optional_sets.append("r.aggregate_value = total_value")
        quality_prop = self._get_job_setting(job_spec, "quality_property")
        if quality_prop:
            safe = sanitize_label(str(quality_prop))
            optional_with.append(
                f"avg(CASE WHEN t.{outcome_prop} = $success_value THEN t.{safe} ELSE null END) AS avg_quality"
            )
            optional_sets.append("r.avg_quality = avg_quality")
        extra_with = "".join(f",\n             {clause}" for clause in optional_with)
        extra_set = "".join(f",\n            {clause}" for clause in optional_sets)

        # Phase 1: positive reinforcement edges
        positive_cypher = f"""
        MATCH (src:{node_label})-[t:{source_edge}]->(dst:{node_label})
        WHERE t.{outcome_prop} IS NOT NULL
        WITH src, dst,
             sum(CASE WHEN t.{outcome_prop} = $success_value THEN 1 ELSE 0 END) AS accepted,
             sum(CASE WHEN t.{outcome_prop} = $failure_value THEN 1 ELSE 0 END) AS rejected,
             max(CASE WHEN t.{outcome_prop} = $success_value THEN t.{ts_prop} ELSE null END) AS last_accept{extra_with}
        WHERE accepted > 0
        MERGE (src)-[r:{positive_edge}]->(dst)
        SET r.count = accepted,
            r.success_rate = toFloat(accepted) / (accepted + rejected),
            r.last_event_date = last_accept,
            r.recency_score = CASE
                WHEN last_accept IS NOT NULL
                THEN exp(-1.0 * duration.inDays(last_accept, datetime()).days / $positive_half_life)
                ELSE 0.0
            END{extra_set}
        RETURN count(r) AS accepted_edges
        """

        # Phase 2: negative signal edges
        reason_prop = self._get_job_setting(job_spec, "reason_property")
        reason_with = (
            f",\n             head(collect(t.{sanitize_label(str(reason_prop))})) AS primary_reason"
            if reason_prop
            else ""
        )
        reason_set = ",\n            r.primary_reason = primary_reason" if reason_prop else ""
        negative_cypher = f"""
        MATCH (src:{node_label})-[t:{source_edge}]->(dst:{node_label})
        WHERE t.{outcome_prop} = $failure_value
        WITH src, dst,
             count(t) AS rejected,
             max(t.{ts_prop}) AS last_reject{reason_with}
        MERGE (src)-[r:{negative_edge}]->(dst)
        SET r.count = rejected,
            r.last_event_date = last_reject,
            r.recency_score = CASE
                WHEN last_reject IS NOT NULL
                THEN exp(-1.0 * duration.inDays(last_reject, datetime()).days / $negative_half_life)
                ELSE 0.0
            END{reason_set}
        RETURN count(r) AS rejected_edges
        """

        # Phase 3: prune stale reinforcement edges — split into separate queries
        prune_positive_cypher = f"""
        MATCH ()-[r:{positive_edge}]-()
        WHERE r.recency_score < $prune_below
        DELETE r
        RETURN count(r) AS pruned
        """
        prune_negative_cypher = f"""
        MATCH ()-[r:{negative_edge}]-()
        WHERE r.recency_score < $prune_below
        DELETE r
        RETURN count(r) AS pruned
        """

        accepted_result = await self.graph_driver.execute_query(positive_cypher, parameters=params, database=db)
        rejected_result = await self.graph_driver.execute_query(negative_cypher, parameters=params, database=db)

        accepted_count = accepted_result[0]["accepted_edges"] if accepted_result else 0
        rejected_count = rejected_result[0]["rejected_edges"] if rejected_result else 0

        from engine.config.settings import settings

        try:
            await self.graph_driver.execute_query(prune_positive_cypher, parameters=params, database=db)
            await self.graph_driver.execute_query(prune_negative_cypher, parameters=params, database=db)
        except Exception as e:
            if settings.gds_prune_failure_partial_enabled:
                # F05: edges are written, the stale-edge prune is not applied.
                logger.exception("Reinforcement prune failed after edges were written")
                return {
                    "status": "partial",
                    "accepted_edges": accepted_count,
                    "rejected_edges": rejected_count,
                    "prune_error": str(e),
                }
            logger.warning(f"Prune step skipped: {e}")

        logger.info(f"Reinforcement: {accepted_count} accepted edges, {rejected_count} rejected edges")
        return {
            "accepted_edges": accepted_count,
            "rejected_edges": rejected_count,
        }

    async def _run_temporal_recency(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """
        Create/update a recency overlay edge over a declared interaction edge.

        For each ``sourceedge`` whose ``timestamp_property`` is within
        ``max_age_days``, MERGE a ``writeedge`` carrying ``last_event_date`` and
        ``recency_score = exp(-age_days / half_life_days)``; overlay edges whose
        score fell below ``prune_below`` are deleted. ``count_property`` (optional)
        names an interaction-count property to carry over as ``recent_count``.

        Required job spec: ``sourceedge``, ``writeedge`` and the
        ``writeproperties`` entry ``timestamp_property``. Optional entries with
        generic defaults: ``half_life_days`` (90), ``max_age_days`` (207),
        ``prune_below`` (0.1).
        """
        db = self.domain_spec.domain.id
        node_label = self._get_candidate_label(job_spec)
        source_edge = self._require_edge(job_spec, "sourceedge")
        overlay_edge = self._require_edge(job_spec, "writeedge")
        ts_prop = sanitize_label(str(self._require_job_setting(job_spec, "timestamp_property")))
        params: dict[str, Any] = {
            "half_life": self._get_job_parameter(job_spec, "half_life_days", 90.0),
            "max_age_days": self._get_job_parameter(job_spec, "max_age_days", 207.0),
            "prune_below": self._get_job_parameter(job_spec, "prune_below", 0.1),
        }
        count_prop = self._get_job_setting(job_spec, "count_property")
        from engine.config.settings import settings

        if settings.gds_temporal_recency_decay_existing:
            return await self._run_temporal_recency_decaying(
                db=db,
                node_label=node_label,
                source_edge=source_edge,
                overlay_edge=overlay_edge,
                ts_prop=ts_prop,
                count_prop=count_prop,
                params=params,
            )
        count_set = (
            f",\n            r.recent_count = CASE WHEN age_days <= $half_life THEN coalesce(t.{sanitize_label(str(count_prop))}, 1) ELSE 0 END"
            if count_prop
            else ""
        )

        create_cypher = f"""
        MATCH (src:{node_label})-[t:{source_edge}]->(dst:{node_label})
        WHERE t.{ts_prop} IS NOT NULL
        WITH src, dst, t,
             duration.inDays(t.{ts_prop}, datetime()).days AS age_days
        WHERE age_days <= $max_age_days
        MERGE (src)-[r:{overlay_edge}]->(dst)
        SET r.last_event_date = t.{ts_prop},
            r.recency_score = exp(-1.0 * age_days / $half_life){count_set}
        RETURN count(r) AS edges_created
        """

        prune_cypher = f"""
        MATCH ()-[r:{overlay_edge}]-()
        WHERE r.recency_score < $prune_below
        DELETE r
        RETURN count(r) AS edges_pruned
        """

        create_result = await self.graph_driver.execute_query(create_cypher, parameters=params, database=db)
        prune_result = await self.graph_driver.execute_query(prune_cypher, parameters=params, database=db)

        created = create_result[0]["edges_created"] if create_result else 0
        pruned = prune_result[0]["edges_pruned"] if prune_result else 0

        logger.info(f"Temporal recency: {created} edges created, {pruned} pruned")
        return {"edges_created": created, "edges_pruned": pruned}

    async def _run_temporal_recency_decaying(
        self,
        *,
        db: str,
        node_label: str,
        source_edge: str,
        overlay_edge: str,
        ts_prop: str,
        count_prop: Any | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """RU-097 / CEG-COR-037: per-pair aggregation and decay of existing edges.

        1. create — one row per (src, dst): the newest interaction within
           ``max_age_days`` is ``last_event_date`` and its age drives
           ``recency_score``; ``recent_count`` (only when ``count_property`` is
           declared) sums ``coalesce(t.<count_property>, 1)`` over the pair's
           interactions within ``half_life_days``.
        2. refresh — every existing overlay edge has ``recency_score``
           recomputed from its own ``last_event_date``, so an edge whose
           interactions aged out keeps decaying instead of freezing.
        3. prune — delete edges whose score fell below ``prune_below`` or whose
           ``last_event_date`` is older than ``max_age_days``.

        Refresh and prune match ``(:node_label)-[r:writeedge]->(:node_label)``,
        the same endpoints the create step MERGEs: the schema does not make
        ``writeedge`` unique per job, so a job sharing the type with another
        job over a different label must not rescale or delete that job's edges
        with its own ``half_life_days`` / ``max_age_days``. The directed pattern
        also counts each pruned relationship once.

        Labels and types arrive sanitized and are re-validated here; every
        value is a ``$param``.
        """
        node_label = sanitize_label(node_label)
        source_edge = sanitize_label(source_edge)
        overlay_edge = sanitize_label(overlay_edge)
        ts_prop = sanitize_label(ts_prop)
        count_agg = (
            ",\n             sum(CASE WHEN age_days <= $half_life "
            f"THEN coalesce(t.{sanitize_label(str(count_prop))}, 1) ELSE 0 END) AS recent_count"
            if count_prop
            else ""
        )
        count_set = ",\n            r.recent_count = recent_count" if count_prop else ""

        create_cypher = f"""
        MATCH (src:{node_label})-[t:{source_edge}]->(dst:{node_label})
        WHERE t.{ts_prop} IS NOT NULL
        WITH src, dst, t,
             duration.inDays(t.{ts_prop}, datetime()).days AS age_days
        WHERE age_days <= $max_age_days
        WITH src, dst,
             max(t.{ts_prop}) AS last_event_date,
             min(age_days) AS newest_age_days{count_agg}
        MERGE (src)-[r:{overlay_edge}]->(dst)
        SET r.last_event_date = last_event_date,
            r.recency_score = exp(-1.0 * newest_age_days / $half_life){count_set}
        RETURN count(r) AS edges_created
        """

        refresh_cypher = f"""
        MATCH (:{node_label})-[r:{overlay_edge}]->(:{node_label})
        WHERE r.last_event_date IS NOT NULL
        WITH r, duration.inDays(r.last_event_date, datetime()).days AS age_days
        SET r.recency_score = exp(-1.0 * age_days / $half_life)
        RETURN count(r) AS edges_refreshed
        """

        prune_cypher = f"""
        MATCH (:{node_label})-[r:{overlay_edge}]->(:{node_label})
        WHERE r.recency_score < $prune_below
           OR (r.last_event_date IS NOT NULL
               AND duration.inDays(r.last_event_date, datetime()).days > $max_age_days)
        DELETE r
        RETURN count(r) AS edges_pruned
        """

        create_result = await self.graph_driver.execute_query(create_cypher, parameters=params, database=db)
        await self.graph_driver.execute_query(refresh_cypher, parameters=params, database=db)
        prune_result = await self.graph_driver.execute_query(prune_cypher, parameters=params, database=db)

        created = create_result[0]["edges_created"] if create_result else 0
        pruned = prune_result[0]["edges_pruned"] if prune_result else 0

        logger.info(f"Temporal recency: {created} edges created, {pruned} pruned")
        return {"edges_created": created, "edges_pruned": pruned}

    async def _run_geoproximity(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """Build proximity edges based on haversine distance.

        Generic graph algorithm: the only vocabulary it needs is the node label,
        the output edge (``writeedge``, default ``COLOCATED_WITH``) and the
        coordinate properties (``lat_property``/``lon_property``). Nodes are paired
        by ``id_property``. The three properties are the domain's declarations
        (Stage 2 follow-up, item 8; the former ``lat``/``lon``/``entity_id``
        defaults return only with declared_structural_properties_required_enabled off).
        """
        db = self.domain_spec.domain.id

        max_km = self._get_job_parameter(job_spec, "max_km", 500.0)
        decay_km = self._get_job_parameter(job_spec, "decay_km", 200.0)
        node_label = self._get_candidate_label(job_spec)
        edge = sanitize_label(job_spec.writeedge or "COLOCATED_WITH")
        lat = sanitize_label(self._declared_job_setting(job_spec, "lat_property", "latitude"))
        lon = sanitize_label(self._declared_job_setting(job_spec, "lon_property", "longitude"))
        id_prop = sanitize_label(self._declared_job_setting(job_spec, "id_property", "geo_id"))

        # Use directional MERGE to avoid duplicate edges
        cypher = f"""
        MATCH (a:{node_label}), (b:{node_label})
        WHERE a.{id_prop} < b.{id_prop}
          AND a.{lat} IS NOT NULL AND b.{lat} IS NOT NULL
        WITH a, b,
             point.distance(
                 point({{latitude: a.{lat}, longitude: a.{lon}}}),
                 point({{latitude: b.{lat}, longitude: b.{lon}}})
             ) / 1000.0 AS dist_km
        WHERE dist_km <= $max_km
        MERGE (a)-[r:{edge}]->(b)
        SET r.distance_km = dist_km,
            r.proximity_score = 1.0 / (1.0 + dist_km / $decay_km)
        RETURN count(r) AS edges_created
        """
        result = await self.graph_driver.execute_query(
            cypher,
            parameters={"max_km": max_km, "decay_km": decay_km},
            database=db,
        )
        edges = result[0]["edges_created"] if result else 0
        logger.info(f"Geo-proximity: {edges} {edge} edges for {node_label}")
        return {"edges_created": edges}

    async def _run_equipment_sync(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """Materialize boolean entity properties as edges to a declared type node.

        For every ``(property, type_name)`` pair in the ``property_mappings``
        entry, an entity whose boolean ``property`` is true gets a ``writeedge``
        to the ``writeto`` node whose ``type_key_property`` (default ``name``)
        equals ``type_name``. Required job spec: ``writeedge``, ``writeto`` and the
        ``writeproperties`` entry ``property_mappings`` — there is no built-in
        mapping table.
        """
        db = self.domain_spec.domain.id
        node_label = self._get_candidate_label(job_spec)
        edge = self._require_edge(job_spec, "writeedge")
        type_label = self._require_edge(job_spec, "writeto")
        type_key = sanitize_label(str(self._get_job_setting(job_spec, "type_key_property") or "name"))
        mappings = self._get_property_mappings(job_spec)

        # Property names are structural and pass sanitize_label; type names are
        # data (they may legitimately contain spaces or dashes) and travel as
        # $parameters, never as quoted literals (C-009).
        type_params: dict[str, Any] = {}
        case_statements = []
        for index, (prop, name) in enumerate(mappings):
            safe_prop = sanitize_label(prop)
            param_key = sanitize_label(f"type_name_{index}")
            type_params[param_key] = name
            case_statements.append(f"CASE WHEN f.{safe_prop} = true THEN ${param_key} END")
        case_list = ",\n            ".join(case_statements)

        cypher = f"""
        MATCH (f:{node_label})
        WITH f, [
            {case_list}
        ] AS type_names
        UNWIND type_names AS type_name
        WITH f, type_name WHERE type_name IS NOT NULL
        MATCH (e:{type_label} {{{type_key}: type_name}})
        MERGE (f)-[:{edge}]->(e)
        RETURN count(*) AS edges_created
        """
        result = await self.graph_driver.execute_query(cypher, parameters=type_params, database=db)
        edges = result[0]["edges_created"] if result else 0
        logger.info(f"Property edge sync: {edges} {edge} edges for {node_label}")
        return {"edges_created": edges}

    def _get_property_mappings(self, job_spec: GDSJobSpec) -> list[tuple[str, str]]:
        """Return the declared ``(boolean_property, type_name)`` pairs for a job.

        Declared under ``writeproperties`` as ``property_mappings: {prop: type}``.
        Fails explicitly when absent or empty — the engine carries no default
        table for any industry.
        """
        raw = self._require_job_setting(job_spec, "property_mappings")
        if not isinstance(raw, dict) or not raw:
            raise ValueError(
                f"Job {job_spec.name}: writeproperties entry 'property_mappings' must be a non-empty mapping "
                "of boolean property name to type name"
            )
        return [(str(k), str(v)) for k, v in raw.items()]

    async def _run_feedback_recalculation(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """Recalculate dimension weights based on accumulated outcomes."""
        from engine.feedback.signal_weights import SignalWeightCalculator, learned_weights_applied

        calculator = SignalWeightCalculator(self.graph_driver, self.domain_spec)
        weights = await calculator.recalculate_weights()
        # CEG-462: a recalculation writes DimensionWeight only under learned_weight_auto_apply_enabled.
        applied = len(weights) if learned_weights_applied() else 0
        logger.info(f"Feedback recalculation: {len(weights)} weights computed, {applied} applied")
        return {"algorithm": "feedback_recalculation", "weights_computed": len(weights), "weights_updated": applied}

    async def _run_causal_chain_scoring(self, job_spec: GDSJobSpec) -> dict[str, Any]:
        """
        Score entities by their causal chain depth and strength.

        Entities that appear in deeper, validated causal chains get higher
        causal_influence_score. This score becomes a scoring dimension
        available to the ScoringAssembler.
        """
        db = self.domain_spec.domain.id
        node_label = self._get_candidate_label(job_spec)
        write_prop = sanitize_label(job_spec.writeproperty or "causal_influence_score")

        # Build edge pattern from causal spec
        causal_spec = self.domain_spec.causal
        depth = int(causal_spec.chain_depth_limit)
        if causal_spec.causal_edges:
            safe_types = [sanitize_label(e.edge_type) for e in causal_spec.causal_edges]
            edge_pattern = "|".join(safe_types)
            rel_pattern = f"[:{edge_pattern}*1..{depth}]"
        else:
            rel_pattern = f"[*1..{depth}]"

        # Calculate causal influence score per entity
        cypher = f"""
        MATCH (n:{node_label})
        OPTIONAL MATCH path = (n)-{rel_pattern}->(effect)
        WITH n,
             count(DISTINCT effect) AS downstream_effects,
             max(length(path)) AS max_chain_depth,
             avg(CASE WHEN length(path) > 0 THEN
                 reduce(conf = 1.0, r IN relationships(path) | conf * coalesce(r.confidence, 0.5))
                 ELSE 0.0 END) AS avg_chain_confidence
        SET n.{write_prop} = CASE
            WHEN downstream_effects > 0
            THEN (toFloat(downstream_effects) * 0.4 +
                  toFloat(coalesce(max_chain_depth, 0)) * 0.3 +
                  coalesce(avg_chain_confidence, 0.0) * 0.3)
            ELSE 0.0
        END
        RETURN count(n) AS nodes_scored
        """
        result = await self.graph_driver.execute_query(cypher, database=db)
        scored = result[0]["nodes_scored"] if result else 0
        logger.info(f"Causal chain scoring: {scored} nodes scored for {node_label}")
        return {"algorithm": "causal_chain_scoring", "nodes_scored": scored}

    async def _run_stable_louvain(self, job_spec: GDSJobSpec, runs: int) -> dict[str, Any]:
        """S2-15: Run Louvain N times and report averaged stability metrics.

        HGKR paper runs each experiment 16 times and averages results.
        Louvain is non-deterministic, so each of the N runs writes its own
        community labels to the same ``writeproperty``: the persisted labels
        are from the last successful run. No per-node consensus is computed
        (community ids are not aligned across runs), and the result says so
        with ``consensus=False`` (RU-099 / CEG-COR-085, DEC-39 option A).

        Args:
            job_spec: GDS job specification.
            runs: Number of stability runs (default from job_spec.stability_runs).

        Returns:
            Result dict with the averaged ``communities`` and ``modularity``,
            ``stability_runs`` (successful runs), ``stability_coefficient``
            (1 - modularity range) and ``consensus`` (always False).
        """
        logger.info(f"Running stable Louvain with {runs} runs for {job_spec.name}")
        all_results: list[dict[str, Any]] = []

        for i in range(runs):
            try:
                result = await self._run_louvain(job_spec)
                all_results.append(result)
            except Exception as e:
                logger.warning(f"Stability run {i + 1}/{runs} failed: {e}")

        if not all_results:
            return {"status": "failed", "error": "All stability runs failed"}

        # Aggregate: average community count and modularity
        avg_communities = sum(r.get("communities", 0) or 0 for r in all_results) / len(all_results)
        avg_modularity = sum(r.get("modularity", 0.0) or 0.0 for r in all_results) / len(all_results)

        return {
            "communities": round(avg_communities),
            "modularity": round(avg_modularity, 6),
            "stability_runs": len(all_results),
            "stability_coefficient": round(
                1.0
                - (
                    max(r.get("modularity", 0.0) or 0.0 for r in all_results)
                    - min(r.get("modularity", 0.0) or 0.0 for r in all_results)
                ),
                6,
            ),
            # Labels on the graph are the last successful run's, not a consensus.
            "consensus": False,
        }

    # ── Lifecycle ──────────────────────────────────────────

    async def trigger_job(self, job_name: str) -> dict[str, Any]:
        """Manually trigger a registered job by name."""
        for job_spec in self.domain_spec.gdsjobs:
            if job_spec.name == job_name:
                return await self.execute_job(job_spec)
        raise ValueError(f"Job '{job_name}' not found in domain spec")

    def start(self) -> None:
        self._started_at = datetime.now(tz=UTC)
        self.scheduler.start()
        logger.info("GDS scheduler started")

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        logger.info("GDS scheduler shut down")

    async def get_job_history(self) -> list[dict[str, Any]]:
        """Get copy of job history (thread-safe)."""
        async with self._history_lock:
            return list(self._job_history)

    @property
    def job_history(self) -> list[dict[str, Any]]:
        """Synchronous access to job history. Use get_job_history() in async contexts."""
        return list(self._job_history)
