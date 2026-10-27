"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, settings]
owner: engine-team
status: active
--- /L9_META ---

engine/config/settings.py

Application settings via pydantic-settings.
Reads from .env file and environment variables.
Single source of truth for all L9_* configuration.
"""

import sys
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRETS = frozenset({"password", "change-me-in-production"})

# Under pytest, ignore a developer's local .env so test suites stay hermetic
# and reflect code-level defaults (or explicit monkeypatch/patch overrides)
# rather than whatever opt-in flags the operator activated for local dev.
# "pytest" is guaranteed to be in sys.modules by the time this module is
# first imported during test collection (pytest imports itself before
# collecting conftest.py / test modules).
_ENV_FILE: str | None = None if "pytest" in sys.modules else ".env"


class Settings(BaseSettings):
    """L9 Engine configuration. All env vars prefixed L9_ unless noted."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Project Identity ---
    l9_project: str = "l9-engine"
    l9_env: str = "dev"

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_database: str = "neo4j"
    neo4j_pool_size: int = 50
    neo4j_max_connection_lifetime: int = 3600
    neo4j_connection_acquisition_timeout: int = 60

    # CEG-007: there was a `redis_url` here, and a redis service in both compose
    # files, and a redis dependency in requirements — and no `import redis`
    # anywhere under engine/ or chassis/. The declared contract claimed scoring
    # and domain-pack caching that was never built. Operators could not start the
    # api service without Redis running, for a dependency nothing used. All of it
    # is removed; if shared state is wanted later, add it with the code that uses it.

    # --- API ---
    api_port: int = 8000
    api_workers: int = 4
    api_secret_key: str = "change-me-in-production"
    cors_origins: list[str] = []  # Default deny-all; set via CORS_ORIGINS env var

    # --- Domain Packs ---
    domains_root: Path = Path("./domains")

    # --- Logging ---
    log_level: str = "INFO"

    # --- GDS ---
    gds_enabled: bool = True

    # --- Scoring Weights (defaults, overridable per-request) ---
    w_structural: float = 0.30
    w_geo: float = 0.25
    w_reinforcement: float = 0.20
    w_freshness: float = 0.10
    max_results: int = 25

    # --- Temporal Decay ---
    # Half-life for causal attribution temporal decay (engine/causal/attribution.py).
    # Stage 2 R10: the former facility/structural half-lives, geo_decay_km,
    # community_cross_bias, outcome_ema_alpha and the melt-flow-index / density
    # resolution tolerances were industry-specific global defaults with no
    # caller anywhere under engine/ or chassis/; they were removed rather than
    # kept as dead configuration. Per-domain tuning belongs in the domain spec.
    decay_transaction_halflife: float = 180.0  # days

    # --- KGE (Phase 4) ---
    kge_enabled: bool = False
    kge_embedding_dim: int = 256  # Must match KGESpec.embeddingdim default (schema.py)
    kge_confidence_threshold: float = 0.3

    # --- Pareto / Multi-Objective ---
    pareto_enabled: bool = True
    pareto_n_samples: int = 50
    pareto_weight_discovery_enabled: bool = False  # off until outcome data flows

    # --- Wave 1: Invariant & Validation Hardening (seL4-inspired) ---
    domain_strict_validation: bool = True  # W1-01: cross-reference validators at load time
    score_clamp_enabled: bool = True  # W1-02: clamp dimension scores to [0, 1]
    strict_null_gates: bool = True  # W1-03: reject gates with null-resolved params
    max_hop_hard_cap: int = 10  # W1-04: maximum hops for traversal patterns
    param_strict_mode: bool = True  # W1-05: raise on derived parameter resolution failures

    # --- Wave 6: Dormant Feature Activation ---
    gdpr_erasure_enabled: bool = False  # W6-02: GDPR erasure endpoint (opt-in)
    gdpr_dry_run: bool = True  # W6-02: when True, compute erasure scope without executing
    gds_max_staleness_hours: int = 25  # W6-03: max hours before GDS job considered stale

    # --- Wave 2: Refinement-Inspired Scoring ---
    feedback_enabled: bool = False  # W2-02: outcome feedback loop (opt-in)
    confidence_check_enabled: bool = True  # W2-03: ensemble confidence bounds
    monoculture_threshold: float = 0.70  # W2-03: single-dimension dominance cap
    ensemble_max_divergence: float = 0.30  # W2-03: GDS/KGE divergence cap (Wave 6)
    score_normalize: bool = False  # W2-04: post-query min-max normalization (opt-in)
    outcome_persistence_enabled: bool = False  # W2-02b: persist outcomes to PacketStore (opt-in)
    # CEG-FIX-001: a RESULTED_IN edge that CausalEdgeRuntimeValidator (R3) rejects
    # is not written; the outcome node still is. Hardening that restores the R3
    # invariant, so it ships on. See docs/FEATURE_GATES.md §18.
    causal_rejection_enforcement_enabled: bool = True
    # CEG-FIX-002: the SDK audit wrapper audits the ingress TransportPacket
    # (Gate's packet_id, trace, correlation, lineage root) instead of a
    # reconstruction. Hardening; read at handler registration. See
    # docs/FEATURE_GATES.md §19.
    ingress_packet_audit_enabled: bool = True

    # --- Wave 3: Capability & Access Control (seL4-inspired) ---
    tenant_auth_enabled: bool = True  # W3-01: JWT allowed_tenants enforcement
    tenant_auth_bypass_key: str = ""  # W3-01: service-to-service bypass key
    # CEG-AUTH-01: under L9_ENV=prod an unset TENANT_ALLOWLIST denies every tenant
    # instead of admitting every tenant. False restores the pre-CEG-AUTH-01
    # behaviour (admit, with a WARNING) for staging or rollback.
    tenant_allowlist_required_in_prod: bool = True
    capability_auth_enabled: bool = True  # W3-02/W3-03: domain-spec capability model

    # --- Wave 4: State Management & Resilience (seL4-inspired) ---
    neo4j_circuit_threshold: int = 5  # W4-02: consecutive failures before circuit opens
    neo4j_circuit_cooldown: float = 30.0  # W4-02: seconds in OPEN before HALF_OPEN
    neo4j_circuit_half_open_max: int = 3  # W4-02: probe calls allowed in HALF_OPEN
    domain_cache_ttl_seconds: int = 30  # W4-03: TTL for async domain pack cache
    domain_cache_maxsize: int = 100  # W4-03: max entries in domain pack cache
    compliance_flush_interval: int = 60  # W4-04: seconds between compliance audit flushes
    compliance_buffer_max: int = 100  # W4-04: max buffered audit entries before forced flush
    # L9-PARTICIPATION-01. On: Gate_SDK owns registration, re-registration, and
    # /v1/ready. Off: CEG's register_from_env loop owns registration so an
    # operator can roll the cutover back. Ships on — the SDK path is what
    # keeps the node routable after a Gate restart. See docs/FEATURE_GATES.md §17.
    sdk_participation_enabled: bool = True
    # Used only while sdk_participation_enabled is False.
    gate_reregistration_enabled: bool = True
    gate_reregistration_interval_seconds: float = 300.0
    postgres_dsn: str | None = None  # W4-04: asyncpg DSN for ComplianceEngine audit-flush pool.
    # None = compliance audit flush is disabled (flush_audit() no-ops with a warning).
    # Distinct from packet_store_dsn below, which feeds PacketStore's own lazy
    # pool — both may point at the same Postgres instance.

    # --- PERSIST-01: PacketStore persistence (engine/packet/packet_store.py) ---
    # These were read with os.environ.get inside packet_store.py, so the only
    # module that persists packets was configured outside the settings SSOT.
    # The env var names are unchanged (PACKET_STORE_ENABLED, PACKET_STORE_DSN,
    # PACKET_STORE_POOL_MIN, PACKET_STORE_POOL_MAX): pydantic-settings matches
    # field names to env vars case-insensitively. Activation prerequisites for
    # production are in docs/FEATURE_GATES.md §7.
    packet_store_enabled: bool = False
    packet_store_dsn: str | None = None
    packet_store_pool_min: int = 2
    packet_store_pool_max: int = 10

    # --- HEALTH-02 / COMM-01 / COMM-02: health commercial model ---
    # The health surface carries a Seed -> Enrich -> Discover -> Autonomous
    # commercial model (TIER_FEATURES, upgrade prompts, conversion events) that
    # nothing in this deployment bills or entitles. The request's self-declared
    # `tier` must never grant capabilities. Off (the default): every call runs
    # with the engine's full health capabilities, and upgrade prompts and
    # conversion-event tracking are suppressed. On: the server-owned
    # `health_entitled_tier` decides the capability set and the conversion
    # funnel is active. See docs/FEATURE_GATES.md §22.
    health_commercial_mode_enabled: bool = False
    # Used only while health_commercial_mode_enabled is True. Must be a key of
    # engine.health.health_schemas.TIER_FEATURES; anything else resolves to seed.
    health_entitled_tier: str = "seed"
    # BATCH-01 / BATCH-02: server-owned ceilings for health_batch_assess. A
    # request may lower either value; it can never raise it above these.
    health_batch_max_entities: int = 100
    health_batch_cost_ceiling_usd: float = 10.0

    # CEG-008: `match` and `sync` route queries to a Neo4j database named after
    # the domain id, and nothing ever created it — Neo4j does not create
    # databases implicitly, so on a fresh instance every sync and match failed
    # with an ExecutionError until an operator ran CREATE DATABASE by hand.
    # With this on, GraphDriver provisions the database on first use (once per
    # database per process). Off by default: CREATE DATABASE is an Enterprise
    # Edition administrative command requiring privileges a read-only
    # deployment is not expected to hold. Either way the failure now names the
    # missing database and the command that provides it.
    auto_create_domain_database: bool = False
    # CEG-006: the `health_*` admin subactions make engine/health/api.py
    # reachable. auto_enrich_via_gate gates only the eventual outbound Gate
    # request, not assessment, reporting or conversion tracking, so the surface
    # itself needs its own gate. Mechanism ships dormant; operator activates.
    health_api_enabled: bool = False
    # CEG-009: some domain packs compile to Cypher that cannot execute (a
    # `RELATES_TO` fallback for gates written with `pattern`/`condition`, scalar
    # `queryparam` values emitted as parameter names such as `$85.0`, or a
    # `strictwhen` no compiler consumes). The loader detects this generically
    # for every pack (engine.config.loader.admission_defects) and withholds the
    # pack. Turning this on serves packs whose gates are known not to execute.
    unvalidated_domain_packs_enabled: bool = False
    # A pack may declare `domain.activation: dormant` to stay withheld until an
    # operator decides otherwise (e.g. a new behavioral surface whose owner has
    # not cleared it for traffic). This is the generic replacement for the
    # per-domain flags the loader used to carry; the engine itself names no
    # domain. Default off: dormant packs are not served.
    dormant_domain_packs_enabled: bool = False

    # --- Wave 7: Explicit Tenant Database Binding ---
    # W7-01 / DB-STRICT-01: GraphDriver.execute_query / execute_write require an
    # explicit database=. Shipped False while callers were audited; the audit
    # (2026-10-22) found every engine caller names its database, so the default
    # is now True and an omitted database= is a programming error, not a silent
    # landing on the shared 'neo4j' database. False restores the fallback.
    strict_tenant_database: bool = True
    # W7-02 / seam audit 2026-09-02: the legacy chassis (dict ExecuteRequest, no Gate
    # provenance, api-key auth) is a direct-ingress side door. Default ON: startup
    # fails unless L9_CHASSIS=sdk outside dev/local/test.
    require_sdk_chassis_in_prod: bool = True

    # --- Stage 2: domain-neutral Cypher (CEG Stage 4 real-Neo4j proof) ---
    # P4 / C-14: a gate's explicitly declared `nullbehavior: pass` was overridden
    # by the per-type default (FAIL for boolean, freshness, traversal) because
    # the schema default is also PASS. On: a declared value wins; undeclared
    # keeps the per-type default. Off: the legacy override. Ships on — the spec
    # is the source of truth (C-12); flip off to reproduce pre-Stage-2 ranking.
    gate_explicit_nullbehavior: bool = True
    # P4: handle_match identified candidates by a hardcoded `entity_id`
    # property (Pareto front ids, causal explanations), a PlasticOS convention.
    # On: the id property is the one the domain declares — the sync endpoint's
    # `idproperty` for the candidate label, else the node's first required
    # property — with `entity_id` as the final fallback. Off: `entity_id` only.
    spec_declared_candidate_id: bool = True
    # P4: the `type: traversal` gate compiled to
    # `exists((candidate)-[:E]->(t:Label {prop: $param}))`, which Neo4j 5
    # rejects ("PatternExpressions are not allowed to introduce new variables"),
    # and its null wrapper tested `candidate.<prop>` although `candidateprop`
    # names the TARGET node's property. On: the gate compiles to
    # `EXISTS { MATCH (candidate)-[:E]->(t:Label) WHERE t.prop = $param }` and
    # null handling checks the query parameter only. Off: the legacy predicate.
    # Ships on — the legacy form cannot execute on any supported Neo4j.
    traversal_gate_exists_subquery: bool = True
    # P3: a `childsync` endpoint copied the child collection onto the parent via
    # `SET n += row` (Neo4j rejects a list of maps as a property) and
    # `childtoparent` emitted `(n)-[:T]<-(c)`, which is not Cypher. On: the
    # parent SET null-overrides child fields, children are created inside
    # FOREACH, and childtoparent emits `(n)<-[:T]-(c)`. Off: the legacy
    # UNWIND-based path. Ships on — the legacy path cannot execute.
    childsync_foreach_projection: bool = True
    # CEG-009b: GateSpec declares `queryparam_min` / `queryparam_max` (range,
    # query-window form) and `candidateprop_start` / `candidateprop_end`
    # (temporalrange, candidate-window form), and GateCompiler ignored all
    # four: a query-window range compiled against the placeholder `$value`
    # and a candidate-window temporalrange against `candidate.timestamp`,
    # which no such candidate carries. On: the declared fields are honoured.
    # Off: the legacy fragments, kept for C-21 rollback only; neither executes
    # faithfully. See docs/FEATURE_GATES.md §21.
    gate_declared_bounds_enabled: bool = True

    # Seam audit / PR remediation: paid-tier enrich_now Gate dispatch is opt-in.
    # Default off so deploy does not immediately spend EIE budget until enabled.
    auto_enrich_via_gate: bool = False
    # EIE-008 / CEG-006: emit `graph-inference-result` to EIE through Gate. EIE
    # advertises and implements the consumer side end to end; nothing here ever
    # produced the packet, so the feedback loop had no producer. Off by default
    # for the same reason as auto_enrich_via_gate — each emission queues
    # re-enrichment targets and so spends EIE budget.
    graph_inference_feedback_enabled: bool = False

    # Correctness remediation lane B (plan 0d84ec8, RU-017..RU-031)
    # RU-017 / CEG-COR-006: pricealignment divided candidate.<p> / $<q>, which
    # Cypher evaluates as INTEGER division when both are integer-stored
    # (450/500 -> 0). On: both operands are wrapped in toFloat(). Off: the
    # legacy expression, for C-21 rollback only.
    pricealignment_float_division: bool = True

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """Raise if default secrets are used in production environment."""
        if self.l9_env == "prod":
            if self.neo4j_password in _DEFAULT_SECRETS:
                msg = "neo4j_password must be changed from default in production"
                raise ValueError(msg)
            if self.api_secret_key in _DEFAULT_SECRETS:
                msg = "api_secret_key must be changed from default in production"
                raise ValueError(msg)
        return self

    @property
    def is_production(self) -> bool:
        return self.l9_env == "prod"

    @property
    def is_development(self) -> bool:
        return self.l9_env == "dev"


# Singleton — import this instance everywhere
settings = Settings()
