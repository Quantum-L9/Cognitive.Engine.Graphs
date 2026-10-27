"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, domain-loader]
owner: engine-team
status: active
--- /L9_META ---

Domain pack loader.
Discovers, validates, caches, and hot-reloads domain spec YAML files.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from engine.config.schema import DomainActivation, DomainSpec
from engine.config.settings import settings
from engine.utils.security import tenant_id_defect

logger = logging.getLogger(__name__)

MAX_SPEC_BYTES = 5 * 1024 * 1024
SPEC_FILENAME = "spec.yaml"

# Cypher parameter names the compiler may emit. Anything else (`$85.0`, `$5`)
# is a scalar `queryparam` that GateSpec coerced to a string and the compiler
# would hand to Neo4j as a parameter *name*.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A `$` reference in compiled Cypher, captured up to the next whitespace or
# closing punctuation. The capture must stop at `}` / `]` / `,` / `;` as well as
# `)`: the traversal-gate target filter form `{prop: $param}` would otherwise
# yield `param}` and withhold a valid pack for a misreported reason (PR #318
# review, verified on Neo4j 5 in the Stage 4 proof).
_PARAM_REF = re.compile(r"\$([^\s)}\],;]+)")


class DomainNotFoundError(Exception):
    """Raised when a requested domain spec does not exist or is withheld."""


class DomainSpecError(Exception):
    """Raised when a domain spec fails validation."""


def admission_defects(spec: DomainSpec) -> list[str]:
    """Gate compilations that would fail or silently reject every candidate.

    This is the generic, domain-neutral admission check that replaced the
    former per-domain feature-flag allowlist (CEG domain-neutrality Stage 2,
    R07). The loader admits a pack only when it validates against ``DomainSpec``
    *and* every gate compiles to Cypher that can execute. Three defect shapes,
    all found when CEG-009 first made the migrated packs readable:

    * ``RELATES_TO`` — the compiler's fallback when a ``type: traversal`` gate
      declares no ``edgetype``. Packs written with ``pattern``/``condition`` hit
      it, and no ontology declares ``RELATES_TO``, so the gate is a hard filter
      that matches nothing.
    * ``strictwhen`` — declared by ``GateSpec`` and consumed by no compiler, so
      a gate meant to be conditional runs as an unconditional hard filter.
    * ``$<non-identifier>`` — a scalar ``queryparam``; since C-009 validates
      parameter names like labels the compiler refuses the gate outright, and
      that refusal is the same defect reported at compile time.

    Making a pack readable is not the same as making it correct. Reaching these
    packs needs either compiler support for pattern/condition, literal operands
    and strictwhen, or a query-schema parameter per constant — a schema
    decision, not a file move. Until then they are withheld, and
    ``settings.unvalidated_domain_packs_enabled`` is the operator's explicit,
    documented override (docs/FEATURE_GATES.md §16).
    """
    from engine.gates.compiler import GateCompiler

    compiler = GateCompiler(spec)
    defects: list[str] = []
    for gate in spec.gates:
        if gate.strictwhen:
            defects.append(f"{gate.name}: strictwhen {gate.strictwhen!r} is not consumed by any compiler")
        try:
            cypher = compiler.compile(gate)
        except (ValueError, KeyError) as exc:
            defects.append(f"{gate.name}: refused by the compiler — {exc}")
            continue
        if "RELATES_TO" in cypher and not gate.edgetype:
            defects.append(f"{gate.name}: RELATES_TO fallback — {cypher}")
        for param in _PARAM_REF.findall(cypher):
            if not _IDENTIFIER.fullmatch(param):
                defects.append(f"{gate.name}: parameter {param!r} is not an identifier — {cypher}")
    # plugins.inference_rules authorizes registered rules by name; a name the
    # registry does not know would surface only when emit_inference_feedback
    # runs it (KeyError), so it is an admission defect, not a runtime surprise.
    if spec.plugins and spec.plugins.inference_rules:
        from engine.inference_rule_registry import list_registered_rules

        registered = set(list_registered_rules())
        for rule_name in spec.plugins.inference_rules:
            if rule_name not in registered:
                defects.append(f"plugins.inference_rules: {rule_name!r} is not a registered inference rule")
    if settings.scoring_admission_check_enabled:
        defects.extend(_scoring_defects(spec, check_producers=True))
    return defects


# A `$name` parameter reference in compiled Cypher (identifiers only; the
# non-identifier form is the separate `$85.0` defect above).
_PARAM_IDENTIFIER_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _bindable_parameters(spec: DomainSpec) -> set[str]:
    """Parameter names ``handle_match`` always binds before executing a match.

    Every queryschema field (backfilled with its default), every derived
    parameter (``ParameterResolver``), every scoring ``queryprop`` (backfilled
    with null for the scoring expressions) and ``top_n``. Values the compilers
    bind themselves come from their ``last_query_params`` and are added by the
    caller.
    """
    names = {field.name for field in spec.queryschema.fields}
    names.update(param.name for param in spec.derivedparameters)
    names.update(dim.queryprop for dim in spec.scoring.dimensions if dim.queryprop)
    names.add("top_n")
    return names


def _no_producer_defect(dim: object) -> str | None:
    """A declared HGKR option whose input nothing in the engine writes (R1b-DEC-01 option A)."""
    from engine.config.schema import ColdStartFallback, NullStrategy

    name = getattr(dim, "name", "?")
    strategy = getattr(dim, "null_strategy", NullStrategy.ZERO)
    if strategy is NullStrategy.INHERIT_PRIOR:
        return f"scoring {name}: null_strategy inherit_prior has no producer (nothing writes candidate._prior_{name})"
    if strategy is NullStrategy.POPULATION_MEAN:
        return f"scoring {name}: null_strategy population_mean has no producer (nothing binds $_popmean_{name})"
    fallback = getattr(dim, "cold_start_fallback", ColdStartFallback.ZERO)
    if fallback is ColdStartFallback.STRUCTURAL_SIMILARITY:
        return (
            f"scoring {name}: cold_start_fallback structural_similarity has no producer "
            "(nothing writes candidate.shared_neighbor_score)"
        )
    if fallback is ColdStartFallback.POPULATION_MEAN:
        return f"scoring {name}: cold_start_fallback population_mean has no producer (nothing binds $_popmean_{name})"
    return None


def _scoring_defects(spec: DomainSpec, *, check_producers: bool) -> list[str]:
    """Scoring dimensions that cannot execute in a declared match direction.

    For each direction, every applicable dimension is compiled exactly as
    ``ScoringAssembler.assemble_scoring_clause`` compiles it: a ``ValueError``
    is a defect quoting the compiler, and every ``$parameter`` the expression
    references must be one ``handle_match`` binds, or the match fails with
    ``ParameterMissing``. One defect per offending dimension.
    """
    from engine.scoring.assembler import ScoringAssembler

    defects: list[str] = []
    bindable = _bindable_parameters(spec)
    if check_producers:
        for dim in spec.scoring.dimensions:
            producer = _no_producer_defect(dim)
            if producer is not None:
                defects.append(producer)
    for direction in spec.queryschema.matchdirections:
        assembler = ScoringAssembler(spec)
        for dim in spec.scoring.dimensions:
            if dim.matchdirections and direction not in dim.matchdirections:
                continue
            try:
                expression = assembler._compile_dimension(dim)
            except (ValueError, KeyError, TypeError) as exc:
                defects.append(f"scoring[{direction}] {dim.name}: refused by the scoring compiler — {exc}")
                continue
            bound = bindable | set(assembler.last_query_params)
            unbound = sorted({p for p in _PARAM_IDENTIFIER_REF.findall(expression) if p not in bound})
            if unbound:
                names = ", ".join(repr(p) for p in unbound)
                defects.append(f"scoring[{direction}] {dim.name}: references parameter(s) {names}, which nothing binds")
        if not any(d.startswith(f"scoring[{direction}]") for d in defects):
            # The clause as handle_match assembles it (aliases, weights, clamp).
            try:
                ScoringAssembler(spec).assemble_scoring_clause(direction, {})
            except (ValueError, KeyError, TypeError) as exc:
                defects.append(f"scoring[{direction}]: refused by the scoring assembler — {exc}")
    return defects


def withholding_reason(spec: DomainSpec) -> str | None:
    """Why a validated pack is not served, or ``None`` when it is admitted.

    Two generic, fail-closed conditions, each with one explicit operator
    override in ``engine/config/settings.py`` and no knowledge of any
    particular domain:

    1. The pack declares itself dormant (``domain.activation: dormant``). The
       authorized domain owner decides when a pack is ready to serve traffic;
       ``dormant_domain_packs_enabled`` lets an operator serve it anyway.
    2. The pack's gates do not compile to executable Cypher
       (``admission_defects``); ``unvalidated_domain_packs_enabled`` serves it
       anyway, knowing the gates will not execute faithfully.
    """
    if spec.domain.activation is DomainActivation.DORMANT and not settings.dormant_domain_packs_enabled:
        return (
            f"Domain '{spec.domain.id}' is declared dormant by its pack (domain.activation: dormant); "
            "set DORMANT_DOMAIN_PACKS_ENABLED=true to serve it"
        )
    if not settings.unvalidated_domain_packs_enabled:
        defects = admission_defects(spec)
        if defects:
            if not _match_admission_checks_enabled():
                return (
                    f"Domain '{spec.domain.id}' is withheld: {len(defects)} gate(s) do not compile to executable "
                    f"Cypher ({defects[0]}); set UNVALIDATED_DOMAIN_PACKS_ENABLED=true to serve it anyway"
                )
            return (
                f"Domain '{spec.domain.id}' is withheld: {len(defects)} admission defect(s) — its gates, traversal "
                f"or scoring do not compile to executable Cypher ({'; '.join(defects)}); "
                "set UNVALIDATED_DOMAIN_PACKS_ENABLED=true to serve it anyway"
            )
    return None


def _match_admission_checks_enabled() -> bool:
    """Whether admission checks more than gates (the pre-remediation wording applies otherwise)."""
    return settings.scoring_admission_check_enabled


class DomainPackLoader:
    """Load and cache folder-shaped domain specs with bounded hot reload."""

    def __init__(self, config_path: str | None = None) -> None:
        raw = config_path or os.getenv("DOMAIN_SPECS_PATH") or "domains"
        self._base_path = Path(raw).resolve()
        self._cache: dict[str, tuple[DomainSpec, float, float]] = {}
        self._lock = threading.Lock()
        self._max_size = int(os.getenv("DOMAIN_CACHE_MAX_SIZE", "100"))
        self._ttl_seconds = float(os.getenv("DOMAIN_CACHE_TTL_SECONDS", "30"))

    def load_domain(self, domain_id: str) -> DomainSpec:
        """Load a domain only when the generic admission policy admits it.

        Admission is decided by ``withholding_reason`` inside
        ``_load_and_validate``, so a withheld pack is never cached as servable
        and the same verdict applies to ``list_domains``.
        """
        spec_path = self._resolve_spec_path(domain_id)

        with self._lock:
            if domain_id in self._cache:
                cached_spec, cached_mtime, cached_at = self._cache[domain_id]
                if (time.monotonic() - cached_at) < self._ttl_seconds:
                    return cached_spec
                current_mtime = spec_path.stat().st_mtime
                if cached_mtime >= current_mtime:
                    self._cache[domain_id] = (cached_spec, cached_mtime, time.monotonic())
                    return cached_spec
                logger.info("Domain spec changed on disk, reloading: %s", domain_id)
            else:
                current_mtime = spec_path.stat().st_mtime

            spec = self._load_and_validate(spec_path, domain_id)
            if len(self._cache) >= self._max_size and domain_id not in self._cache:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][2])
                del self._cache[oldest_key]
                logger.debug("Evicted oldest domain cache entry: %s", oldest_key)

            self._cache[domain_id] = (spec, current_mtime, time.monotonic())
            return spec

    def invalidate(self, domain_id: str | None = None) -> None:
        """Force cache invalidation."""
        with self._lock:
            if domain_id:
                self._cache.pop(domain_id, None)
            else:
                self._cache.clear()

    async def load_domain_async(self, domain_id: str) -> DomainSpec:
        """Async domain loading with per-domain stampede prevention."""
        with self._lock:
            if domain_id in self._cache:
                cached_spec, _cached_mtime, cached_at = self._cache[domain_id]
                if (time.monotonic() - cached_at) < self._ttl_seconds:
                    return cached_spec

        if not hasattr(self, "_async_locks"):
            self._async_locks: dict[str, asyncio.Lock] = {}
        if domain_id not in self._async_locks:
            self._async_locks[domain_id] = asyncio.Lock()

        async with self._async_locks[domain_id]:
            with self._lock:
                if domain_id in self._cache:
                    cached_spec, _cached_mtime, cached_at = self._cache[domain_id]
                    if (time.monotonic() - cached_at) < self._ttl_seconds:
                        return cached_spec
            return await asyncio.to_thread(self.load_domain, domain_id)

    def list_domains(self) -> list[str]:
        """Discover admitted domain directories containing spec.yaml.

        A directory is listed only when its pack loads under the same
        fail-closed admission policy ``load_domain`` applies, so nothing this
        method names can later be refused as unadmitted. Packs that fail
        validation or are withheld are logged and omitted — never served.
        """
        if not self._base_path.is_dir():
            return []
        admitted: list[str] = []
        for d in sorted(self._base_path.iterdir()):
            if not (d.is_dir() and (d / SPEC_FILENAME).exists()):
                continue
            try:
                self.load_domain(d.name)
            except (DomainNotFoundError, DomainSpecError) as exc:
                logger.info("Domain pack %s not admitted: %s", d.name, exc)
                continue
            admitted.append(d.name)
        return admitted

    def _resolve_spec_path(self, domain_id: str) -> Path:
        """Resolve and validate spec file path, preventing traversal and symlinks.

        The id is checked against the tenant-id grammar before any path is built
        (CEG-IDENT-01 / CEG-DBNAME-01): tenant id == domain.id == directory ==
        Neo4j database name, so an id the database could not be named by is
        refused here, fail closed, and never reaches the filesystem.
        """
        if not domain_id or not domain_id.strip():
            raise DomainNotFoundError("Domain ID cannot be empty")
        if "\x00" in domain_id:
            raise DomainNotFoundError(f"Invalid domain ID: {domain_id!r} contains null byte")
        if Path(domain_id).is_absolute():
            raise DomainNotFoundError(f"Invalid domain ID: {domain_id!r} must be a relative path")
        defect = tenant_id_defect(domain_id)
        if defect is not None:
            raise DomainNotFoundError(f"Invalid domain ID: {defect}")

        candidate = (self._base_path / domain_id / SPEC_FILENAME).resolve()
        raw_path = self._base_path / domain_id / SPEC_FILENAME
        if raw_path.is_symlink():
            raise DomainNotFoundError(f"Invalid domain path: {domain_id!r} spec.yaml is a symlink")
        try:
            candidate.relative_to(self._base_path.resolve())
        except ValueError as exc:
            raise DomainNotFoundError(f"Invalid domain path: {domain_id!r} resolves outside base directory") from exc
        if not candidate.exists():
            raise DomainNotFoundError(f"Domain spec not found: {candidate}")
        return candidate

    def _load_and_validate(self, path: Path, domain_id: str) -> DomainSpec:
        """Load YAML and validate against DomainSpec schema."""
        file_size = path.stat().st_size
        if file_size > MAX_SPEC_BYTES:
            raise DomainSpecError(
                f"Domain spec {domain_id} exceeds maximum size: {file_size} bytes > {MAX_SPEC_BYTES} bytes"
            )
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise DomainSpecError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise DomainSpecError(f"Domain spec must be a YAML mapping, got {type(raw).__name__}")
        try:
            spec = DomainSpec.model_validate(raw)
        except PydanticValidationError as exc:
            raise DomainSpecError(f"Domain '{domain_id}' validation failed:\n{exc}") from exc
        if spec.domain.id != domain_id:
            raise DomainSpecError(
                f"Domain '{domain_id}' declares domain.id {spec.domain.id!r}; the directory name is the lookup key"
            )
        reason = withholding_reason(spec)
        if reason is not None:
            raise DomainNotFoundError(reason)
        return spec
