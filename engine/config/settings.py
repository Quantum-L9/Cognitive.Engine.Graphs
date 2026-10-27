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

    # Correctness remediation lane A (plan 0d84ec8, RU-001..RU-060)
    # Each flag ships on (a correctness fix); off restores the pre-fix behaviour.
    # RU-001 / CEG-COR-001: handle_match RETURNs the active dimension scores as a
    # `dimension_scores` map so Pareto, arbitration and confidence checks see
    # real values. Off: `RETURN candidate, score`.
    match_dimension_projection: bool = True
    # RU-011 / CEG-COR-033: `WITH *` between the traversal clauses and the gate
    # WHERE so gates filter candidate rows even after a trailing OPTIONAL MATCH
    # (whose own WHERE only nulls optional variables). Off: WHERE follows the
    # traversal clauses directly.
    gate_where_row_scope: bool = True
    # RU-036 / CEG-COR-007: a candidate with k matching traversal paths produced
    # k rows and LIMIT counted rows. On: after scoring, keep each candidate's
    # highest-scoring row (`head(collect(...))` over score-ordered rows) before
    # ORDER BY / LIMIT. Off: one row per path.
    match_collapse_traversal_rows: bool = True
    # RU-032 / CEG-COR-167: the W2-03 monoculture check reads weight x value per
    # dimension (ScoringAssembler.last_effective_weights), its share of the score,
    # instead of raw dimension values. Off: raw values (annotate_candidates).
    monoculture_weighted_contribution: bool = True
    # RU-016 / CEG-COR-171: handle_match backfills queryschema defaults before
    # resolving derived parameters, so a derivation over an omitted defaulted
    # field sees its default. Off: resolve first, backfill after.
    query_defaults_before_derived: bool = True
    # RU-009 / CEG-COR-018: match `weights` are validated as the per-dimension
    # weights scoring applies for the direction (override, else spec default),
    # after the domain loads; unknown weight keys are rejected (R1a-DEC-06).
    # Off: only the supplied keys are summed, before the domain loads.
    match_effective_weight_validation: bool = True
    # RU-004 / CEG-COR-014: legacy resolve raises ValidationError when the CREATE
    # returned no row (source or target not found) instead of reporting
    # `resolved`. Off: `resolved` regardless of the result.
    resolve_requires_created_edge: bool = True
    # RU-005 / CEG-COR-056: handle_outcomes stores map-valued fingerprint fields
    # (dimension_weights) as sorted-key JSON the counterfactual reader decodes.
    # Off: the str(dict) writer (the reader stays tolerant of both).
    outcome_map_json_encoding: bool = True
    # RU-013 / CEG-COR-074: when the probed tenant has no pack, handle_health
    # reports the number of loaded packs instead of listing every domain id.
    # Off: the id list.
    health_probe_domain_count_only: bool = True
    # RU-060 / CEG-COR-169: UNWINDMATCHSET sync statements RETURN count(n) and
    # handle_sync reports that as synced_count plus unmatched_count. Off: no
    # RETURN and synced_count = len(batch).
    sync_count_from_write_result: bool = True
    # RU-003 / CEG-COR-003: every domain-bearing admin subaction's domain_id must
    # equal the packet tenant (identity contract; mirrors HEALTH-05); a differing
    # value is rejected before any load or query, and evaluate matches as the
    # tenant. Off: the caller-chosen domain_id.
    admin_tenant_bound_domain: bool = True
    # Correctness remediation lane B (plan 0d84ec8, RU-017..RU-031)
    # RU-017 / CEG-COR-006: pricealignment divided candidate.<p> / $<q>, which
    # Cypher evaluates as INTEGER division when both are integer-stored
    # (450/500 -> 0). On: both operands are wrapped in toFloat(). Off: the
    # legacy expression, for C-21 rollback only.
    pricealignment_float_division: bool = True
    # RU-018 / CEG-COR-025: arbitration helpers (pareto_integrator) looked up a
    # dimension with `or`, so a genuine 0.0 fell through to `<d>_score` or was
    # treated as absent and skipped its hard constraint. On: presence is tested
    # with `is None` (dimension_scores[d] -> d -> d_score). Off: legacy truthiness.
    arbitration_zero_score_present: bool = True
    # RU-019 / CEG-COR-026: the decision_arbitration Pareto front maximised
    # every objective and was unbounded. On: ParetoObjectiveSpec.direction
    # 'minimize' is honoured and the front is trimmed to
    # ParetoConfigSpec.front_size_limit by composite score (R1b-DEC-04 A).
    # Off: maximise-all, unbounded.
    arbitration_objective_direction: bool = True
    # RU-022 / CEG-COR-030: ScoringAssembler.load_learned_weights read
    # DimensionWeight by `dimension_name` / `domain_id`, properties the writer
    # (SignalWeightCalculator, keyed {dimension, tenant}) never sets, so learned
    # weights were always {}. On: the reader filters on {tenant: $tenant} and
    # dw.dimension. Off: the legacy (inert) reader. FEEDBACK_ENABLED stays the
    # global gate (default False).
    learned_weights_tenant_keyed: bool = True
    # RU-023 / CEG-COR-083: learned multipliers (up to signal_weights.max_weight)
    # pushed the positive weight sum past the C-22 ceiling, and a learned 0.0
    # was dropped by a truthiness filter. On: a 0.0 row is loaded (and takes
    # the penalty branch) and the non-penalised coefficients are rescaled to
    # their pre-learning sum. Off: multiply-only, truthiness filter.
    learned_weight_renormalization: bool = True
    # RU-027 / CEG-COR-116: SignalWeightCalculator computed the CI as
    # 1.96*sqrt(lift*(1-min(lift,1))/n), which is 0.0 for every lift >= 1, so a
    # single winning outcome got confidence 1.0. On: the CI of the win-rate
    # proportion (Agresti-Coull), divided by the base win rate. Off: legacy.
    signal_weight_proportion_ci: bool = True
    # RU-028 / CEG-COR-117: ScorePropagator matched each prior outcome of the
    # candidate and then every DimensionWeight, so one event multiplied
    # boost_factor by factor^k and reported k*|dw| affected, unbounded. On:
    # outcomes are collapsed to one existence row, SET runs once per
    # DimensionWeight and the result is clamped to signal_weights
    # [min_weight, max_weight]. Off: the legacy query.
    propagation_single_application: bool = True
    # RU-029 / CEG-COR-118: DriftDetector treated only a both-empty window pair
    # as insufficient data, so an empty baseline (the first drift_window_days)
    # scored divergence 1.0 and raised alert=True. On: either window empty is
    # insufficient_data (with both counts). Off: legacy both-empty only.
    drift_requires_both_windows: bool = True
    # Correctness remediation lane C (plan 0d84ec8, RU-020..)
    # RU-020 / CEG-COR-027..029: admission_defects also compiles every declared
    # direction's scoring clause and withholds a pack whose scoring refuses to
    # compile, references a $parameter nothing binds, or declares an HGKR option
    # with no producer (inherit_prior / population_mean / structural_similarity).
    # Such a pack failed every match (ValueError / ParameterMissing) or silently
    # degraded. Off: admission checks gates only, as before. Ships on — it
    # withholds only packs whose matches cannot execute.
    scoring_admission_check_enabled: bool = True
    # RU-039: a pack admission serves must execute for every declared match
    # direction. On: admission_defects also requires TraversalAssembler to
    # assemble each direction and every $parameter in gates, traversal and
    # scoring to be one handle_match binds (queryschema field, derived
    # parameter, scoring queryprop, top_n or a compiler-bound literal). Off:
    # the previous checks only. Ships on (fail-closed, Stage-2 precedent).
    admission_checks_full_match: bool = True
    # RU-041 / CEG-COR-019: an exclusion gate always anchors its edge at the
    # matched candidate. One declaring fromnode/tonode, or whose ontology edge
    # does not start at a candidate label of its direction(s), compiled to a
    # NOT EXISTS that is always true (mortgage blacklist_exclusion). On: the
    # compiler refuses such a gate, so admission withholds the pack. Off: the
    # legacy always-true fragment.
    exclusion_endpoint_validation: bool = True
    # RU-042 / CEG-COR-020, CEG-COR-021: a gate's null policy was applied to
    # `candidate.<candidateprop>` and `$<queryparam>` whatever the predicate
    # tested — range FAIL passed NULL bounds, derived-bound range and
    # candidateprop exclusion null-checked a property they never read, and
    # temporalrange checked an undeclared `$<queryparam>`. On: each handler
    # reports the operands it tests and the wrap applies PASS (a NULL operand
    # imposes no constraint on its side) or FAIL (any NULL rejects) to exactly
    # those. Off: the previous fragments.
    gate_null_operands_from_predicate: bool = True
    # RU-043 / CEG-COR-022: a composite's subgates were also compiled as
    # top-level filters (an OR composite degenerated to AND), and a subgate
    # scoped to another direction was compiled inside the composite anyway.
    # On: subgates referenced by a composite applicable to the direction are
    # skipped at top level (compile_all_gates and compile_relaxed), and a
    # subgate scoped away from the direction being compiled is refused.
    # Off: the legacy compilation. Specs without composites are unaffected.
    composite_subgate_scoping: bool = True
    # RU-052 / CEG-COR-079: freshness `maxagedays` defaulted to 1 for any
    # falsy value (0 -> 1 day) and a negative age compiled to a cutoff in the
    # future. On: the default applies only when maxagedays is absent, and a
    # value < 1 is refused at compile, so admission withholds the pack. Off:
    # the legacy coercion.
    freshness_maxagedays_validated: bool = True
    # Correctness remediation lane D (plan 0d84ec8, RU-061..RU-077)
    # RU-061 / CEG-COR-008: the prohibited-factor check compared only
    # GateSpec.candidateprop/queryparam, exact-case. On: every compiled
    # property reference (gate min/max/start/end fields, scoring dimension
    # candidateprop/queryprop, sync-row keys at any depth) is compared
    # casefolded. Off: the legacy two-field exact-case check.
    prohibited_factor_full_coverage: bool = True
    # RU-062 / CEG-COR-009: PIIHandler detection descended only into dicts, so
    # PII inside lists (match `candidates`, childsync rows) was never redacted,
    # and redact()/mask_fields() mutated the caller's nested dicts. On: lists
    # and tuples are traversed with index-aware paths (`a[0].b`) and edits copy
    # every container on the edited path. Off: dict-only, legacy copies.
    pii_redaction_recursive: bool = True
    # RU-065 / CEG-COR-052/053: the compliance audit buffer grew without bound
    # when no Postgres pool was configured, a configured pool failed every
    # flush (metadata dict bound to JSONB) and handle_match / handle_sync
    # re-raised that failure after the Neo4j work succeeded. On: the buffer is
    # bounded by compliance_buffer_max (oldest dropped, count logged; every
    # entry is still emitted to the l9.audit log), metadata is bound as JSON
    # text, and a request-path flush failure is logged, not raised. Off:
    # unbounded retention and propagation.
    compliance_audit_soft_sink: bool = True
    # RU-063 / CEG-COR-050: ProhibitedFactorsSpec.auditonviolation was read
    # nowhere, so a blocked attempt left no audit trail. On: every violation
    # raised through ComplianceEngine first emits one CRITICAL `denied` entry
    # (regimes as compliance_tags, gate/dimension/endpoint/row path and the
    # field name, never a value) when the domain's auditonviolation is true.
    prohibited_factor_violation_audit: bool = True
    # RU-064 / CEG-COR-051: ComplianceEngine probed a nonexistent
    # `compliance.pii.additional_fields`, so declared PII fields that no
    # built-in hint matched survived raw and `handling: hash` became deletion.
    # On (with pii_redaction_recursive): when compliance.pii.enabled, declared
    # fields + candidatefields are PII by exact case-insensitive key at any
    # depth, and every detected field is transformed per PIISpec.handling
    # (hash -> SHA-256 digest, redact -> fixed marker, encrypt/tokenize fail
    # closed to the marker). Off: built-in inference with key deletion only.
    pii_declared_fields_enforced: bool = True
    # RU-067 / CEG-COR-055: attribution weighted one share per PATH row, so a
    # touchpoint reached by several paths (diamonds, parallel edges) split or
    # overwrote credit (linear summed to 0.6, first_touch to 0). On: rows are
    # collapsed to unique touchpoint ids keeping the minimum-distance row,
    # whose ages feed temporal decay. Off: per-row weighting.
    causal_attribution_unique_touchpoints: bool = True
    # RU-068 / CEG-COR-057: CausalCompiler matched source and target
    # by a hardcoded `entity_id` (an outcome node has only `outcome_id`),
    # emitted `AND $confidence >= <literal>` without a WHERE when temporal
    # validation was off, and MERGEd on `created_at: datetime()` so every
    # repeat created a parallel edge. On: declared identities (`outcome_id`
    # for the outcome label), one WHERE with `$confidence_threshold`, MERGE on
    # the type with ON CREATE / ON MATCH SET; AttributionCalculator reads each
    # touchpoint's declared id per label (PO-04). Off: the legacy Cypher.
    causal_edge_declared_identity: bool = True
    # RU-069 / CEG-COR-058: CausalEdgeRuntimeValidator looked a
    # CausalSubgraphSpec edge up by record["edge_type"], which sync records do
    # not carry, so declared temporal_validation was skipped for every sync
    # batch. On: the batch edge_type is used. Off: the record key.
    causal_validator_batch_edge_type: bool = True
    # RU-076 / CEG-COR-125: safe_eval ignored keyword arguments, so
    # round(2.567, ndigits=2) returned 3 and max(x, key=y) returned x. On: a
    # call with keywords raises ValueError. Off: keywords silently dropped.
    safe_eval_reject_keywords: bool = True
    # RU-074 / CEG-COR-121: CausalSubgraphSerializer matched `[r:T]` (one
    # hop), so match explanations never went deeper than 1. On: `[:T*1..N]`
    # with N = max_depth clamped to [1, causal.chain_depth_limit]. Off: the
    # fixed one-hop pattern.
    causal_serializer_variable_depth: bool = True
    # RU-075 / CEG-COR-122: CounterfactualGenerator emitted the same
    # (change_weight, <dim>_weight_increase) once per qualifying positive and
    # queried a hardcoded :TransactionOutcome. On: duplicates suppressed and
    # the domain's feedbackloop.outcome_node_label used. Off: legacy.
    counterfactual_dedupe_and_spec_label: bool = True
    # Correctness remediation lane E (plan 0d84ec8, RU-078..RU-090)
    # RU-078 / CEG-COR-040, -095: PacketStore keyed packet_store.content_hash
    # on the payload hash, so two requests with an equal payload collided, the
    # second pair's lineage insert broke the FK and the whole pair rolled back.
    # Child rows were also rewritten for a packet whose INSERT was skipped. On:
    # content_hash is the SDK transport_hash (packet identity) and a skipped
    # packet writes no hop/delegation/lineage rows. Off: the legacy key and
    # unconditional child rows (C-21 rollback).
    packet_store_packet_identity_dedup_enabled: bool = True
    # RU-079 / CEG-COR-042: packet_store.sql's RLS policies compare rows to
    # current_setting('app.current_tenant') and nothing set it, so a DSN that
    # connects as an l9_product member had every write rejected (swallowed).
    # On: persist and record_outcome run `set_config('app.current_tenant',
    # <tenant>, true)` inside their transaction before writing. Off: no GUC.
    packet_store_rls_tenant_session_enabled: bool = True
    # RU-084 / CEG-COR-043: infer_geography_from_postal_code bucketed the
    # five-digit ZIP with a threshold ladder (30301 Atlanta -> Midwest, 60601
    # Chicago -> South) and labelled foreign postcodes (75001 Paris) with a US
    # region. On: USPS first-digit zones and a declared non-US `country`
    # yields no inference. Off: the legacy ladder.
    inference_geo_usps_zones_enabled: bool = True
    # RU-088 / CEG-COR-097: infer_buyer_persona matched keywords as substrings
    # ('Marketing Coordinator' -> executive via 'coo', 'DevOps Engineer' ->
    # operations via 'ops'). On: whole title tokens only. Off: substrings.
    inference_persona_token_match_enabled: bool = True
    # RU-089 / CEG-COR-098: infer_company_size_tier used `or` to pick a field
    # (employee_count=0 fell through to employees) and parsed both values in
    # one try (an unparseable employee count discarded a valid revenue), and
    # infer_email_domain_from_website kept the port ('acme.com:8443'). On:
    # is-None fallback, per-field parsing, and the URL hostname. Off: legacy.
    inference_input_parsing_strict_enabled: bool = True
    # RU-086 / CEG-COR-093: the SDK audit wrapper (chassis/handler_registration
    # .py) recorded a failed pair for a raising handler but not for one the SDK
    # cancelled on timeout (CancelledError bypasses `except Exception`). On:
    # the cancelled call persists one ('<action>', 'failed') pair under
    # asyncio.shield, then the cancellation is re-raised. Off: no pair.
    packet_audit_on_cancel_enabled: bool = True
    # RU-090 / CEG-COR-099: CapabilityValidator.validate_action checked the
    # presented object, so a forged Capability reusing a registered id, an
    # unexpired copy of an expired record, or an in-place `expires_at = 0`
    # edit all validated. On: the object must equal the registered record and
    # activity is read from the record; expires_at enters proof_hash (the
    # admin delegate_capability response therefore shows a new proof_hash).
    # Off: the legacy checks and hash input.
    capability_record_binding_enabled: bool = True
    # Correctness remediation lane F (plan 0d84ec8, RU-093..RU-103)
    # RU-094 / CEG-COR-035: a HALF_OPEN probe cancelled before it produced a
    # result (the SDK runtime's wait_for timeout) returns its probe slot. Off:
    # the slot leaks and three cancelled probes wedge the breaker until restart.
    neo4j_circuit_release_cancelled_probes: bool = True
    # RU-093 / CEG-COR-034: neo4j ClientError (syntax, constraint, missing
    # database, null MERGE key) is a per-request fault and no longer counts
    # toward opening the process-wide Neo4j breaker. Off: every exception counts.
    neo4j_circuit_ignore_client_errors: bool = True
    # RU-095 / CEG-COR-170: with auto_create_domain_database on, breaker
    # admission happens before first-use provisioning, so an OPEN breaker
    # rejects without attempting CREATE DATABASE. Off: provision, then admit.
    neo4j_provision_inside_breaker: bool = True
    # RU-096 / CEG-COR-036: the GDS execution DAG is keyed by job name, so two
    # jobs sharing an algorithm both schedule. Off: algorithm-keyed DAG (the
    # last same-algorithm job declared silently replaces the others).
    gds_dag_job_identity_by_name: bool = True
    # RU-097 / CEG-COR-037: the temporal-recency overlay aggregates per
    # (src, dst) (newest interaction wins), re-scores every existing overlay
    # edge each run, and prunes on score OR age > max_age_days. Off: the
    # per-row create and score-only prune, under which aged edges never decay.
    gds_temporal_recency_decay_existing: bool = True
    # RU-098 / CEG-COR-168: co-occurrence power-law pruning orders each node's
    # edges by weight (elementId tie-break) and deletes an edge only when both
    # endpoints prune and neither keeps it. Off: unordered per-endpoint prune
    # that can leave hubs below their keep count and differs run to run.
    gds_cooccurrence_deterministic_prune: bool = True
    # RU-100 / CEG-COR-086: GDS job history timestamps (and the job start /
    # duration clock) are timezone-aware UTC, so staleness is the same under
    # any process TZ. Off: naive local time, which consumers read as UTC.
    gds_history_utc_timestamps: bool = True
    # RU-101 / CEG-COR-059..061: entity resolution threads and validates the
    # batch threshold, verifies each member against the chosen canonical,
    # MERGEs RESOLVED_FROM (at most one per pair, no cycles) and skips batch
    # entities by entity id. Off: the previous merge behaviour. The path is also
    # gated per domain by semantic_registry.enabled (default False).
    entity_resolution_merge_integrity: bool = True
    # RU-103 / CEG-COR-177: behavioral similarity matches both entities with
    # the entity label and the label's declared id property (PO-04; that part
    # also follows spec_declared_candidate_id). Off: label-less entity_id match,
    # which counts outcomes of same-id nodes of other labels.
    resolution_behavioral_label_scoped: bool = True
    # Correctness remediation lane G (plan 0d84ec8, RU-108..)
    # RU-108 / CEG-COR-044: readiness coerced an absent field confidence to 0,
    # so a fully populated entity with no confidence map scored 0.0/F, and
    # health_batch_assess / health_report dropped a supplied map. On: a
    # populated field counts unless an explicit confidence < 0.70 discounts
    # it, batch/report forward `field_confidences`, and blocking_fields names
    # exactly the uncounted gate fields. Off: the legacy coercion.
    health_absent_confidence_counts_enabled: bool = True
    # RU-109 / CEG-COR-045: build_field_map marked only `candidateprop` as
    # gate-critical, so a range gate's candidateprop_min/max (and a
    # temporalrange's candidateprop_start/end) were "low" and an entity missing
    # every bound scored 95/A. On: all five declared keys are gate-critical.
    # Off: candidateprop only.
    health_range_gate_fields_enabled: bool = True
    # RU-110 / CEG-COR-046: a blocked readiness (gate completeness < 0.50)
    # reported gate_score * 100, so gc=0.49 scored 49.0 while gc=0.50 could
    # score 40.0. On: the blocked score is the 60% gate component
    # round(gate_score * 60, 2), always < 30. Off: gate_score * 100.
    health_blocked_score_scale_enabled: bool = True
    # RU-111 / CEG-COR-047: compute_enrichment_priority's documented 10%
    # staleness term read a staleness_penalty attribute EntityHealth does not
    # have, so it was always 0. On: the term is
    # compute_staleness_penalty(entity_health.field_health). Off: the zero term.
    health_priority_staleness_enabled: bool = True
    # Correctness remediation lane I (plan 0d84ec8, RU-044..RU-057)
    # RU-044 / CEG-COR-024: the default-weight ceiling summed each scope bucket
    # alone, so {'*': 0.6, 'd1': 0.6} loaded with an effective d1 sum of 1.2.
    # On (with domain_strict_validation): every direction's sum includes the
    # unscoped dimensions. Off: per-bucket check.
    weight_ceiling_includes_unscoped: bool = True
    # RU-045 / CEG-COR-038: the hop validator read `*1..` as one hop, so `*1..`
    # and `*2..` loaded and were emitted unbounded. On: a `..` needs an explicit
    # upper bound (checked against max_hop_hard_cap) and the assembler grammar
    # requires `*N` or `*[M]..N`. Off: legacy parse and grammar.
    traversal_open_range_rejected: bool = True

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
