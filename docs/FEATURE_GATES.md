<!-- L9_META
l9_schema: 2
origin: engine-specific
engine: graph
layer: [docs]
tags: [platform]
status: active
/L9_META -->

# Feature Gates — Activation Runbook

<!--
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [feature-gates, activation, runbook]
owner: engine-team
status: active
--- /L9_META ---
-->

This document describes every gated feature in the Graph Cognitive Engine,
its current activation state, prerequisites, activation steps, validation,
and rollback procedure.

> **Principle**: seL4-inspired mechanism/policy separation. The *mechanism*
> (code) ships dormant. The *policy* (operator decision) activates it via
> environment variables. Nothing activates by default.

---

## Quick Reference

The **Default** column is the code-level default in `engine/config/settings.py`
(what ships if no environment variable is set — the seL4 "dormant mechanism"
baseline). The **Local `.env`** column reflects this repo's checked-in-locally,
gitignored `.env` (see `.env.template`), which the operator may activate
independently of the code default.

| Feature | Flag | Default | Local `.env` | State |
|---|---|---|---|---|
| KGE (CompoundE3D) | `KGE_ENABLED` | `False` | `True` (dim=300) | active (opt-in) |
| GDPR Erasure | `GDPR_ERASURE_ENABLED` | `False` | `True` | active (dry-run) |
| GDPR Dry-Run | `GDPR_DRY_RUN` | `True` | `True` | active — keep `True` until an operator explicitly opts into live erasure |
| GDS Scheduler | `GDS_ENABLED` | `True` | `True` | active |
| GDS Staleness Probe | `GDS_MAX_STALENESS_HOURS` | `25` | `25` | active |
| Score Normalization | `SCORE_NORMALIZE` | `False` | `True` | active (opt-in) |
| Outcome Feedback | `FEEDBACK_ENABLED` | `False` | `True` | active (opt-in; two-level gate — see §5) |
| Confidence Checking | `CONFIDENCE_CHECK_ENABLED` | `True` | `True` | active |
| Pareto Ensemble | `PARETO_ENABLED` | `True` | `True` | active |
| Pareto Weight Discovery | `PARETO_WEIGHT_DISCOVERY_ENABLED` | `False` | `True` | active (opt-in) |
| Domain Strict Validation | `DOMAIN_STRICT_VALIDATION` | `True` | `True` | active |
| Score Clamping | `SCORE_CLAMP_ENABLED` | `True` | `True` | active; a null dimension clamps to `0.0` as well (Stage 2, see §20) |
| Strict Null Gates | `STRICT_NULL_GATES` | `True` | `True` | active |
| Param Strict Mode | `PARAM_STRICT_MODE` | `True` | `True` | active |
| LLM Security (ValidatedLLMClient) | `LLM_PROVIDER` | `openai` (implicit) | `openai` | active — fully implemented, requires `OPENAI_API_KEY` |
| PacketStore Persistence | `PACKET_STORE_ENABLED` (`packet_store_enabled`), `PACKET_STORE_DSN` (`packet_store_dsn`) | `False` | `True` | active (opt-in; read through `Settings` since PERSIST-01 — see §7) |
| Health Commercial Mode | `HEALTH_COMMERCIAL_MODE_ENABLED` (`health_commercial_mode_enabled`), `HEALTH_ENTITLED_TIER` | `False`, `seed` | unset | dormant; tier entitlements, upgrade prompts and conversion events — see §22 |
| Health Batch Ceilings | `HEALTH_BATCH_MAX_ENTITIES`, `HEALTH_BATCH_COST_CEILING_USD` | `100`, `10.0` | unset | active; server-owned caps a request may lower but not raise — see §22 |
| Tenant Allowlist Required in Prod | `TENANT_ALLOWLIST_REQUIRED_IN_PROD` (`tenant_allowlist_required_in_prod`) | `True` | unset | active (hardening); an unset `TENANT_ALLOWLIST` denies every tenant under `L9_ENV=prod` — see §11 |
| Gate Declared Bounds | `GATE_DECLARED_BOUNDS_ENABLED` (`gate_declared_bounds_enabled`) | `True` | unset | active; range honours `queryparam_min`/`queryparam_max`, temporalrange honours `candidateprop_start`/`candidateprop_end` — see §21 |
| Strict Tenant Database | `STRICT_TENANT_DATABASE` (`strict_tenant_database`) | `True` | unset | active (hardening); an omitted `database=` on `GraphDriver` raises — see §23 |
| Outcome Persistence | `OUTCOME_PERSISTENCE_ENABLED` | `False` | `True` | active (opt-in; requires PacketStore — see §5) |
| Tenant Auth (JWT allowed_tenants) | `TENANT_AUTH_ENABLED` | `True` | `True` | active in the chassis only — claim decoded, not enforced; `TENANT_ALLOWLIST` is the enforced control — see §11 |
| Capability Auth (domain-spec model) | `CAPABILITY_AUTH_ENABLED` | `True` | `True` | active |
| PostgreSQL Audit Pool | `POSTGRES_DSN` | unset (`None`) | set | active (opt-in, soft dependency — see §7) |
| Dormant Domain Packs | `DORMANT_DOMAIN_PACKS_ENABLED` (`dormant_domain_packs_enabled`) | `False` | unset | dormant; serve packs that declare `domain.activation: dormant` (replaces the per-domain `idea_portfolio_enabled`) — see §16 |
| Graph Inference Feedback | `GRAPH_INFERENCE_FEEDBACK_ENABLED` (`graph_inference_feedback_enabled`) | `False` | unset | dormant; CEG → Gate → EIE `graph-inference-result` — see §13 |
| Domain Database Provisioning | `AUTO_CREATE_DOMAIN_DATABASE` (`auto_create_domain_database`) | `False` | unset | dormant; create the tenant domain database on first use — see §14 |
| Health API (admin health_* subactions) | `HEALTH_API_ENABLED` (`health_api_enabled`) | `False` | unset | dormant; AI-readiness assess/report surface — see §15 |
| Unvalidated Domain Packs | `UNVALIDATED_DOMAIN_PACKS_ENABLED` (`unvalidated_domain_packs_enabled`) | `False` | unset | dormant; seven packs whose gates do not compile to executable, faithful Cypher — see §16 |
| SDK Participation | `SDK_PARTICIPATION_ENABLED` (`sdk_participation_enabled`) | `True` | unset | active; Gate_SDK owns registration and readiness; off restores CEG's loop — see §17 |
| Gate Re-registration | `GATE_REREGISTRATION_ENABLED` (`gate_reregistration_enabled`), `GATE_REREGISTRATION_INTERVAL_SECONDS` | `True`, `300` | unset | active only while SDK participation is off — see §17 |
| Causal Rejection Enforcement | `CAUSAL_REJECTION_ENFORCEMENT_ENABLED` (`causal_rejection_enforcement_enabled`) | `True` | unset | active; an R3-rejected RESULTED_IN edge is not written — see §18 |
| Ingress Packet Audit | `INGRESS_PACKET_AUDIT_ENABLED` (`ingress_packet_audit_enabled`) | `True` | unset | active; SDK audit pairs record Gate's ingress packet — see §19 |
| Constellation Orchestration | — | — | — | accepted architectural gap — see §9 |
| Gate Explicit Null Behavior | `GATE_EXPLICIT_NULLBEHAVIOR` (`gate_explicit_nullbehavior`) | `True` | unset | active; a gate's declared `nullbehavior` wins over the per-type default (C-14) — see §20 |
| Spec-declared Candidate Id | `SPEC_DECLARED_CANDIDATE_ID` (`spec_declared_candidate_id`) | `True` | unset | active; `handle_match` identifies candidates by the domain's declared id property, not `entity_id` — see §20 |
| Traversal Gate EXISTS Subquery | `TRAVERSAL_GATE_EXISTS_SUBQUERY` (`traversal_gate_exists_subquery`) | `True` | unset | active; traversal gates compile to an `EXISTS { … }` subquery Neo4j 5 accepts — see §20 |
| Childsync FOREACH Projection | `CHILDSYNC_FOREACH_PROJECTION` (`childsync_foreach_projection`) | `True` | unset | active; childsync writes children in `FOREACH` without copying them onto the parent — see §20 |

---

## 1. KGE — CompoundE3D Embeddings

**State**: Dormant
**Flag**: `KGE_ENABLED=True`
**Settings**: `kge_embedding_dim` (default 256), `kge_confidence_threshold` (default 0.3)

### Prerequisites
- Domain spec must include a `kge:` section with valid `embeddingdim` and `trainingrelations`.
- Neo4j vector index must exist (matching dimension and cosine similarity).
- Embedding dimension in domain spec must match `kge_embedding_dim` setting.

### Activation Steps
1. Set `KGE_ENABLED=True` in environment.
2. Ensure domain spec has `kge:` section.
3. Call admin subaction `trigger_kge` with `domain_id` to activate.
4. Verify with `kge_status` subaction.

### Validation
- `kge_status` returns `enabled: true` with config details.
- `trigger_kge` smoke test confirms vector index is reachable.

### Rollback
- Set `KGE_ENABLED=False`. KGE scoring dimensions return 0.0.
- No data deletion needed — embeddings remain in Neo4j but are unused.

---

## 2. GDPR Erasure

**State**: Dormant
**Flag**: `GDPR_ERASURE_ENABLED=True`
**Settings**: `GDPR_DRY_RUN=True` (default — compute scope without executing)

### Prerequisites
- Domain spec must have `compliance.pii` section with field declarations.
- Caller must hold `admin:gdpr` capability (when capability auth is enabled).
- Recommend running dry-run first (`GDPR_DRY_RUN=True`).

### Activation Steps
1. Set `GDPR_ERASURE_ENABLED=True` in environment.
2. Keep `GDPR_DRY_RUN=True` initially for safe validation.
3. Call `erase_subject` admin subaction with `data_subject_id`.
4. Review dry-run report: nodes affected, edges affected.
5. Set `GDPR_DRY_RUN=False` and re-run for actual deletion.

### Validation
- Dry-run returns `{"dry_run": true, "would_affect": {...}}`.
- Real run returns `{"status": "erased", "summary": {...}}`.
- Audit trail contains `PII_ERASURE` entry at CRITICAL severity.

### Rollback
- Set `GDPR_ERASURE_ENABLED=False`. Endpoint returns disabled status.
- Erasure is irreversible — restore from backup if needed.

---

## 3. GDS Job Management

**State**: Active (scheduler runs automatically when `GDS_ENABLED=True`)
**Flag**: `GDS_ENABLED=True` (default)
**Settings**: `GDS_MAX_STALENESS_HOURS=25` (health probe threshold)

### Admin Subactions
- `gds_status` — returns per-algorithm run history, last status, next scheduled.
- `gds_trigger` — manually triggers a single algorithm run by name.
- `gds_health` — checks whether algorithms have run within staleness window.

### Prerequisites
- Domain spec must have `gdsjobs:` section with algorithm definitions.
- Neo4j must be reachable for GDS algorithm execution.

### Activation Steps
1. GDS is active by default. Use `gds_status` to inspect.
2. Use `gds_trigger` for initial population after deployment.
3. Adjust `GDS_MAX_STALENESS_HOURS` if algorithms run less frequently.

### Validation
- `gds_status` shows successful runs with timestamps.
- `gds_health` returns `healthy` when all algorithms are within staleness window.

---

## 4. Score Calibration (W2-01)

**State**: Active
**Admin Subaction**: `calibration_run`

Score calibration runs against the domain spec's `calibration.pairs` section.
No feature flag needed — available whenever calibration pairs are defined.

---

## 5. Outcome Feedback (W2-02)

**State**: Dormant
**Flag**: `FEEDBACK_ENABLED=True`

Enables the outcome feedback convergence loop. When active, outcome records
(positive/negative/neutral) influence scoring dimension weights over time.

---

## 6. Score Normalization (W2-04)

**State**: Dormant
**Flag**: `SCORE_NORMALIZE=True`

Post-query min-max normalization of match scores to [0, 1] range.

---

## 7. PostgreSQL Persistence

**State**: Active (opt-in, soft dependency)
**Flag**: `POSTGRES_DSN` (unset by default — `None` disables audit flush)
**Prerequisites**: PostgreSQL instance provisioned (docker-compose `postgres`
service, or managed instance).

Two independent, lazily-initialized `asyncpg` pools point at PostgreSQL —
by design they are not merged, even though they may target the same instance:

| Pool | Config | Consumer | Behavior when unset |
|---|---|---|---|
| ComplianceEngine audit-flush | `POSTGRES_DSN` (`settings.postgres_dsn`) | `engine/compliance/audit.py: AuditLogger.flush_to_store()` | `flush_audit()` no-ops with a warning log — never blocks startup |
| PacketStore | `PACKET_STORE_ENABLED` (`settings.packet_store_enabled`) + `PACKET_STORE_DSN` (`settings.packet_store_dsn`); pool size `PACKET_STORE_POOL_MIN` / `PACKET_STORE_POOL_MAX` (`settings.packet_store_pool_min` / `_max`, defaults 2 / 10) | `engine/packet/packet_store.py` | `PacketStore` disabled — persistence calls no-op |

### PacketStore configuration (PERSIST-01)

`engine/packet/packet_store.py` used to read its four variables with
`os.environ.get`, so the only module that persists packets was configured
outside the settings SSOT and invisible to `feature_status`, `.env` loading and
the production-secret validator. They are now `Settings` fields with the same
environment variable names (pydantic-settings matches field names to env vars
case-insensitively, so nothing an operator has set changes). `.env.template`
already carries `PACKET_STORE_ENABLED` and `PACKET_STORE_DSN`; the two pool-size
variables are documented here rather than added to that protected file.

### Production activation prerequisites (PacketStore)

Enabling persistence in production requires, in this order:

1. A PostgreSQL instance reachable from the engine with
   `engine/packet/packet_store.sql` applied (the docker-compose `postgres`
   service applies it on first init only; a managed instance needs it run by
   hand), including migration `0001_outcome_jsonb` for `record_outcome()`.
2. `PACKET_STORE_DSN` set to that instance with a role that can `INSERT` into
   `packet_store`, `lineage_graph`, `hop_trace`, `delegation_chain` and
   `UPDATE packet_store.outcome_jsonb`. The DSN is a secret: provide it from the
   deployment's secret store, never from a committed file.
3. `asyncpg` installed in the image (`PacketStore` raises `RuntimeError`
   naming the package otherwise).
4. `PACKET_STORE_ENABLED=true`. With it on and the DSN unset, the first persist
   raises `RuntimeError("PACKET_STORE_DSN ... is required")` — the store does
   not degrade silently to disabled.
5. Only then `OUTCOME_PERSISTENCE_ENABLED=true` (§10), which depends on this
   store.

Compose changes that provision or wire the store are infrastructure and belong
to a separately approved change (`CONTRACT-05`); this section documents the
contract they must satisfy.

### Wiring

1. `engine/boot.py: GraphLifecycle.startup()` reads `settings.postgres_dsn`;
   if set, creates an `asyncpg.create_pool(...)` (min_size=1, max_size=5). If
   the DSN is unset, or the pool fails to connect, `db_pool` stays `None` and
   a warning is logged — the container starts regardless (same graceful-
   degrade pattern as the Neo4j connection above it).
2. `db_pool` is passed to `init_dependencies(graph_driver, domain_loader,
   db_pool)` in `engine/handlers.py`, which stores it on the `EngineState`
   singleton (`engine/state.py: EngineState.db_pool`).
3. `_get_compliance_engine()` reads `state.db_pool` when constructing each
   domain's `ComplianceEngine`, so the periodic compliance-flush loop
   (`engine/boot.py: _compliance_flush_loop`) writes to `packet_audit_log`
   instead of warning every interval.
4. `GraphLifecycle.shutdown()` → `EngineState.shutdown()` closes the pool.

### Schema

Single source of truth: `engine/packet/packet_store.sql` — defines
`packet_store`, `lineage_graph`, `hop_trace`, `delegation_chain`, and
`packet_audit_log` tables. Mounted into the `postgres` docker-compose
service at `/docker-entrypoint-initdb.d/01_packet_store.sql` (applied
automatically on first container init only).

### Validation
- `feature_status` / `EngineState.health_check()` reports `db_pool_present: true`.
- No `"No db_pool configured"` warning in logs after the flush interval elapses.

### Rollback
- Unset `POSTGRES_DSN` (or `PACKET_STORE_ENABLED=false`). Both pools are
  soft dependencies — the engine runs unaffected, audit flush/packet
  persistence simply no-op with a warning.

---

## 8. LLM Security (ValidatedLLMClient)

**State**: Active — fully implemented (not a stub)
**Flag**: `LLM_PROVIDER` (default `"openai"` even if unset), `OPENAI_API_KEY` (required)

> **Correction**: earlier revisions of this doc and `DEFERRED.md: DEFERRED-002`
> described this as a stub requiring SDK integration. Inspection of
> `engine/security/P2_9_llm_schemas.py` confirms `_LLMBackend` and
> `ValidatedLLMClient` are fully wired to the OpenAI SDK — this was already
> shipped, just undocumented as active.

Input sanitization and output schema validation wrap the actual `_call()`
integration, which calls the OpenAI SDK using `OPENAI_API_KEY`. Raises
`FeatureNotEnabled("LLM SDK", flag="OPENAI_API_KEY")` only when the key is
missing — the provider selection itself (`LLM_PROVIDER=openai` |
`openai-compatible`) does not gate the feature.

### Activation Steps
1. Set `OPENAI_API_KEY` in environment (required — no default).
2. `LLM_PROVIDER` defaults to `openai`; set explicitly for clarity or to use
   an OpenAI-compatible endpoint.

### Rollback
- Unset `OPENAI_API_KEY`. Any LLM-backed call raises `FeatureNotEnabled`
  cleanly rather than failing at the network layer.

---

## 9. Constellation Orchestration

**State**: Accepted architectural gap (documented, not scheduled)

PacketEnvelope protocol, delegation chains, and hop traces are fully
implemented in `engine/packet/`, but multi-node Constellation routing
(Gate-authored dispatch across independently-deployed engine nodes) is not
exercised by this repo's test suite or CI. Single-node deployment — the only
topology this repo runs today — uses the chassis bridge directly
(`engine/boot.py: GraphLifecycle.execute()` → `chassis.actions.execute_action`),
never a cross-node hop.

### Why this is accepted, not a bug
- This repo (`Cognitive.Engine.Graphs`) is one **engine node**. It is not the
  routing authority — `constellation-gate` is (see that repo's `AGENTS.md`
  for the routing-law invariants: Gate is sole routing authority, worker
  dispatch must be Gate-authored, node-origin traffic must target Gate).
- Exercising real multi-node orchestration requires a second live engine
  node plus a Gate instance — infrastructure this repo does not provision
  and should not fake with local mocks that could drift from the real
  routing contract.
- The SDK chassis (`create_node_app()`) already registers this node with
  Gate at startup and keeps it registered (L9-PARTICIPATION-01),
  so the node-side half of the integration is live; what remains dormant is
  *receiving* Gate-dispatched cross-node requests, which requires an actual
  Constellation deployment to test end-to-end.

### What would close the gap
- A staging Constellation deployment (Gate + ≥2 engine nodes) with an
  integration test that dispatches a cross-node request through Gate and
  asserts `provenance.resolved_by_gate == true` on receipt.
- Out of scope for a single-repo change — requires coordination with
  `constellation-gate` and is tracked as a cross-repo follow-up, not a
  `DEFERRED.md` entry local to this repo.

---

## 10. Outcome Persistence (W2-02b)

**State**: Dormant
**Flag**: `OUTCOME_PERSISTENCE_ENABLED=True`
**Prerequisites**: PacketStore reachable (`PACKET_STORE_ENABLED`, `PACKET_STORE_DSN`).

Writes match outcomes through to the PacketStore. This is a second gate on top of
`FEEDBACK_ENABLED`: the feedback loop can run in-memory without it, and enabling it
without a reachable PacketStore degrades to logged warnings rather than hard failure.

---

## 11. Tenant Auth (W3-01) and the tenant allowlist (CEG-AUTH-01)

**State**: `TENANT_AUTH_ENABLED` active in the chassis; `TENANT_ALLOWLIST` is the
enforced control
**Flag**: `TENANT_AUTH_ENABLED=True` (default on)

What `TENANT_AUTH_ENABLED` does today: `chassis/auth/auth.py` decodes the JWT
`allowed_tenants` claim onto `request.state` and honours the service-to-service
bypass key. It does **not** compare the claim against the request tenant — the
middleware defers that "to the handler layer", and no caller ever forwarded the
claim into the engine. The engine-side branch of `_validate_tenant_access` that
would have compared it was therefore unreachable; CEG-AUTH-01 removed it rather
than keep code that documented an enforcement which did not exist. Wiring the
claim through the execute route is a chassis change and is not done here.

The control that is enforced on every action is `TENANT_ALLOWLIST`
(comma-separated tenant ids, resolved into `EngineState` at startup):

| `TENANT_ALLOWLIST` | `L9_ENV` | Behaviour |
|---|---|---|
| set | any | tenants not listed are rejected (`ValidationError`, 422) |
| unset | `dev` / `local` / `test` / `staging` | every tenant admitted, with a WARNING at startup |
| unset | `prod` | **every tenant rejected** (fail closed) and an ERROR at startup naming the fix |

Before CEG-AUTH-01 the last row admitted every tenant with only a warning. A
production deployment that forgot the allowlist now refuses requests instead of
running open.

**Flag**: `TENANT_ALLOWLIST_REQUIRED_IN_PROD` (`tenant_allowlist_required_in_prod`),
default `True`. **Rollback**: set it to `false` to restore the pre-CEG-AUTH-01
last row (admit every tenant, WARNING at startup) without inventing an allowlist.

---

## 12. Capability Auth (W3-02 / W3-03)

**State**: Active
**Flag**: `CAPABILITY_AUTH_ENABLED=True` (default on)

Enforces the domain-spec capability model, mapping each action to the permissions it
requires. Disabling it removes per-action authorization while leaving tenant resolution
intact.

---

## 13. Graph Inference Feedback (EIE-008 / CEG-006)

**State**: Dormant
**Flag**: `GRAPH_INFERENCE_FEEDBACK_ENABLED=False` (default off)

Emits `graph-inference-result` to Enrichment.Inference.Engine through Gate.
EIE advertises that action to Gate and implements the whole consumer side —
packet validation, per-tenant queues, target extraction, a 0.55 confidence
floor, injection into the convergence loop — and nothing in CEG ever produced
the packet, so the loop had a consumer and no producer. Both sides were even
built to the same confidence floor.

### Prerequisites

- `GATE_URL` configured and Gate reachable; `graph-inference-result` is owned by
  `eie` in Gate's `CANONICAL_ACTION_OWNERS`, so Gate resolves the destination.
- EIE registered with Gate advertising `graph-inference-result`.

### Activation Steps

1. Set `GRAPH_INFERENCE_FEEDBACK_ENABLED=true`.
2. Drive the `admin` subaction `emit_inference_feedback` with an `entity`,
   an `entity_id`, and optionally a `rules` list (defaults to every registered
   inference rule).

### Validation

The dispatch result carries `sent_outputs`: how many findings cleared the 0.55
floor and were actually sent. `status: "skipped"` with
`no_outputs_above_confidence_floor` means nothing qualified and no packet was
sent — an empty `inference_outputs` list is valid to EIE and would cost a Gate
round trip to queue nothing.

### Rollback

Set the flag back to `False`. Each emission queues re-enrichment targets in EIE
and therefore spends EIE budget, which is why it ships off — the same reason as
`AUTO_ENRICH_VIA_GATE`.

---

## 14. Domain Database Provisioning (CEG-008)

**State**: Dormant
**Flag**: `AUTO_CREATE_DOMAIN_DATABASE=False` (default off)

`match` and `sync` route queries to a Neo4j database named after the domain id.
Neo4j does not create databases implicitly, so on a fresh instance every sync
and match failed with an `ExecutionError` until an operator ran
`CREATE DATABASE` by hand. With this flag on, `GraphDriver` provisions the
database on first use — once per database per process.

### Prerequisites

- **Neo4j Enterprise Edition.** `CREATE DATABASE` is an Enterprise
  administrative command; Community Edition rejects it.
- Credentials with database administration privileges.

### Activation Steps

1. Set `AUTO_CREATE_DOMAIN_DATABASE=true`.
2. No restart of Neo4j is required; the next query against an unprovisioned
   domain creates it.

### Validation

`Ensured Neo4j database '<id>' exists` is logged on the provisioning call.
A refusal (Community Edition, or missing privilege) is logged as a warning and
does **not** raise: a deployment whose database already exists is never blocked
by a CREATE it is not allowed to run.

### Rollback

Set the flag back to `False`. Provisioning stops; a query against an absent
database then raises `DatabaseNotProvisionedError`, which names the missing
database and the exact `CREATE DATABASE` command. That message is present
whether or not the flag is on.

---

## 22. Health Commercial Mode (HEALTH-02 / COMM-01 / COMM-02) and batch ceilings (BATCH-01 / BATCH-02)

**State**: Dormant
**Flags**: `HEALTH_COMMERCIAL_MODE_ENABLED=False` (default off),
`HEALTH_ENTITLED_TIER=seed`, `HEALTH_BATCH_MAX_ENTITIES=100`,
`HEALTH_BATCH_COST_CEILING_USD=10.0`

The health surface (§15) carries a Seed → Enrich → Discover → Autonomous
commercial model: `TIER_FEATURES` in `engine/health/health_schemas.py`, upgrade
prompts, and a Seed → Enrich conversion funnel. Nothing in this deployment
bills or entitles a tenant, and the tier used to be whatever the request
declared — so any caller could claim `autonomous` and receive auto-enrichment
(an EIE spend), while Seed callers were upsold and recorded in a funnel for a
product that cannot be bought.

One resolver, `resolve_effective_capabilities()` in `health_schemas.py`, now
decides every health call's capabilities. The request's `tier` is echoed back
as `requested_tier` and grants nothing, in either mode.

| Mode | Effective tier | Upgrade prompts | Conversion events |
|---|---|---|---|
| off (default) | full capability set (`autonomous` row, with `conversion_tracking` forced off) | never | never |
| on | `HEALTH_ENTITLED_TIER` (server-owned; unknown value → `seed`, logged) | yes | per the entitled tier's `TIER_FEATURES` row |

The enum, `TIER_FEATURES` and the report helpers are retained; only who
selects the row changed. Auto-enrichment is still separately gated by
`AUTO_ENRICH_VIA_GATE` (the outbound Gate request), and every assess response
now carries `enrichment_dispatch` — the full `trigger_reenrichment_v2` result —
with `reason` equal to `dispatch.error` on every non-dispatched path
(HEALTH-03).

### Batch ceilings

`health_batch_assess` accepts `max_entities` and `cost_ceiling_usd` as
*requests*. Each is bounded above by `HEALTH_BATCH_MAX_ENTITIES` /
`HEALTH_BATCH_COST_CEILING_USD`; a request may lower a ceiling and never raise
it, and a negative or non-numeric value is rejected (422). The report's buckets
partition the request: `total_scanned` + `total_skipped` + `total_truncated`
= `total_candidates`; `estimated_eligible` replaces `total_enriched` (a batch
scan enriches nothing) and `dispatched` is `0` until a dispatch path exists.

### Activation Steps

1. Decide the entitlement source. `HEALTH_ENTITLED_TIER` is a deployment-wide
   value; per-tenant entitlement needs a lookup that does not exist yet.
2. Set `HEALTH_COMMERCIAL_MODE_ENABLED=true` and `HEALTH_ENTITLED_TIER`.
3. The conversion-event store is in-memory and bounded; a persistent funnel
   store is a prerequisite for reading the metrics in anger.

### Rollback

Set the flag back to `False`. Prompts stop, events stop, and every call runs
with the full capability set again.

---

## 23. Strict Tenant Database (W7-01 / DB-STRICT-01)

**State**: Active (hardening; default flipped to on 2026-10-22)
**Flag**: `STRICT_TENANT_DATABASE=True` (default on)

Every tenant's data lives in a Neo4j database named after its domain id, so a
`GraphDriver.execute_query` / `execute_write` call that names no `database=`
can only mean the caller forgot — and the old silent fallback put that caller's
reads and writes on the shared `neo4j` database. The flag shipped `False` while
callers were audited; the audit found every engine caller names its database
(handlers, GDS scheduler, feedback, resolution, causal, scoring, sync), so the
default is now `True` and an omitted `database=` raises a `ValueError` that
names the method and this flag.

`handle_health`'s liveness probe passes `database="neo4j"` explicitly and is
unaffected. Live GDPR erasure (`erase_subject` admin subaction with
`GDPR_DRY_RUN=false`) passes the subject's domain database — the caller's
`domain_id`, the same value its dry-run count queries — so it runs under the
strict default without an opt-out.

### Rollback

`STRICT_TENANT_DATABASE=false` restores the `neo4j` fallback.

---

## 15. Health API (CEG-006)

**State**: Dormant
**Flag**: `HEALTH_API_ENABLED=False` (default off)

Exposes `engine/health/api.py` through the `admin` subactions `health_assess`,
`health_batch_assess` and `health_report`. That module implemented three
handlers and was imported by nothing, so `request_enrichment` — the whole
CEG → Gate → EIE direction — had no trigger an inbound packet could reach.

`AUTO_ENRICH_VIA_GATE` is not a substitute: it gates only the eventual outbound
Gate request, not assessment, reporting, or conversion-event tracking. The
surface needs a gate of its own, which is what this is.

### Validation

With the flag off the subactions return `{"status": "disabled"}` rather than
404-ing, so an operator can tell "switched off" from "not a subaction".

### Identity (HEALTH-05)

Tenant id, domain spec id and Neo4j database name are one identity — there is
no mapping layer (operator decision 2026-10-22). `payload.domain` may be absent
or equal to the packet tenant; any other value is rejected by `handle_admin`
before a domain pack is loaded, and again by `engine/health/api.py` for direct
callers. A health call cannot read another tenant's spec.

### Rollback

Set back to `False`. Note that assess/report append to the conversion-event
store in `engine/health/health_report.py` only while commercial mode (§22) is
on; that store is bounded (`CONVERSION_EVENT_MAX`) and discards oldest-first.

---

## 16. Domain Pack Admission: Unvalidated and Dormant Packs (CEG-009 / Stage 2 R07)

**State**: Dormant
**Flags**: `UNVALIDATED_DOMAIN_PACKS_ENABLED=False`, `DORMANT_DOMAIN_PACKS_ENABLED=False` (both default off)

The loader admits a pack through one generic, fail-closed policy
(`engine.config.loader.withholding_reason`) that names no domain:

1. the pack validates against `DomainSpec`;
2. the pack does not declare `domain.activation: dormant` — or the operator set
   `DORMANT_DOMAIN_PACKS_ENABLED=true`;
3. every gate compiles to executable Cypher (`admission_defects`) — or the
   operator set `UNVALIDATED_DOMAIN_PACKS_ENABLED=true`.

A withheld pack is neither listed by `list_domains` nor loadable; the refusal
names the reason and the override. This replaced the former per-domain flag
allowlist (`idea_portfolio_enabled`, and the seven CEG-009 packs named in the
loader). The same packs are withheld as before — `idea-portfolio` because its
pack now declares itself dormant, the seven below because their gates still do
not compile — but the engine no longer knows any of them by name, and an
unknown but valid pack is admitted on the same terms.

CEG-009 moved nine domain packs out of a flat `<name>_domain_spec.yaml` shape
the loader never reads into `domains/<id>/spec.yaml`. Making them readable
exposed that seven do not compile to executable, faithful Cypher:

| Pack | Defect |
|---|---|
| `executive-assistant` | `RELATES_TO` fallback + `$85.0` |
| `repo-as-agent` | `RELATES_TO` fallback + `$5` |
| `roofing-company` | `RELATES_TO` fallback + `$1` |
| `aios-god-agent` | `RELATES_TO` fallback |
| `healthcare-referral` | `$1` |
| `legal-discovery` | `strictwhen` on a gate, consumed by no compiler |
| `research-agent` | `strictwhen` on two gates, consumed by no compiler |

Three root causes, all in the spec-to-Cypher contract rather than in the files:

1. `type: traversal` gates written with `pattern` and `condition`, which
   `GateCompiler` does not consume. With no `edgetype` the compiler falls back
   to `RELATES_TO`, an edge no ontology here declares, so the gate is a hard
   filter that matches nothing.
2. A scalar `queryparam` (`85.0`, `5`, `1`). `GateSpec.coerce_queryparam_to_str`
   turns it into a string; since C-009 validates parameter names like labels
   the compiler refuses the gate outright instead of emitting `$85.0`.
3. `strictwhen` on a gate. `GateSpec` declares it and nothing under `engine/`
   consumes it, so a gate the author wrote as conditional (`legal-discovery`,
   `research-agent`) runs as an unconditional hard filter.

**Do not turn this on to "see if they work".** They do not: the first serves
zero candidates, the second fails at execution. Fixing them means either
compiler support for `pattern`/`condition` and literal operands, or a declared
query-schema parameter per constant — a schema decision, not a file move.

`tests/unit/test_domain_pack_shape.py` holds both ends: an admitted pack must
compile to executable Cypher, and every withheld pack must still have a live
generic reason (a compile defect or a declared dormant posture). A pack fixed
later is served automatically — there is no per-domain list to forget it in.

---

## 17. Gate Re-registration

Gate keeps its node registry in memory, and its health monitor only probes
nodes that are already registered — it never discovers an unregistered one.
A single startup registration therefore leaves this node unroutable after any
Gate restart (observed on the Constellation Docker rail: after `docker restart`
of Gate, `match` answered `404 no node registered` until CEG itself restarted),
or when Gate was unreachable at CEG's startup.

`sdk_participation_enabled` (default `True`, hardening) gives Gate
participation to Gate_SDK (L9-PARTICIPATION-01). `chassis/node_app.py` then
calls `create_node_app(auto_register_with_gate=True)`: the SDK registers at
startup from `GATE_NODE_SPEC_PATH`, re-registers every
`GATE_REREGISTRATION_INTERVAL_SECONDS` (default `300`; `0` registers once
only), each attempt `overwrite=True` so repeats are idempotent, and answers
`GET /v1/ready` 503 while Gate has not accepted the node. `GET /v1/health`
stays a liveness probe and reports the participation state.
`GATE_REGISTRATION_ENABLED=false` turns the SDK registration off.

`SDK_PARTICIPATION_ENABLED=false` rolls the cutover back. GraphLifecycle then
runs `engine/gate_registration.py`, and `gate_reregistration_enabled` plus
`gate_reregistration_interval_seconds` (default `300`) bound that loop. The
SDK chassis passes `auto_register_with_gate=False` on that path so the two
loops do not both register.

---

## 18. Causal Rejection Enforcement (CEG-FIX-001)

When a domain sets `causal.enabled`, `handle_outcomes` runs
`CausalEdgeRuntimeValidator` (R3) over the outcome's RESULTED_IN edge record
(`source_ts`, `target_ts`, `confidence`, judged against the domain's
`RESULTED_IN` `CausalEdgeSpec`). Before CEG-FIX-001 a rejection was only
logged, and both write paths still created the edge.

`causal_rejection_enforcement_enabled` (default `True`, hardening) makes the
rejection binding. A rejected edge is not created by either write path, the
`TransactionOutcome` node is still recorded — the validator judges the edge,
not the outcome observation (`docs/contracts/data/graph-schema.yaml`) — and
the response carries `rejected_count`, matching `handle_sync`.

Two consequences follow from the domain's causal configuration:

- the edge record names `RESULTED_IN`, so `CausalEdgeSpec.temporal_validation`
  is applied: `source_ts` must strictly precede `target_ts` when both are given;
- with `causal.enabled` and no `RESULTED_IN` entry in `causal.causal_edges`,
  every outcome edge is rejected as `invalid_edge_type`.

Domains with `causal.enabled=false` are unaffected: the validator is not
consulted and the edge is written as before.

`CAUSAL_REJECTION_ENFORCEMENT_ENABLED=false` restores the log-only behaviour.

---

## 19. Ingress Packet Audit (CEG-FIX-002)

On the SDK chassis (`L9_CHASSIS=sdk`), `chassis/handler_registration.py` wraps
every engine handler so each call persists a request/response packet pair to
PacketStore. Before CEG-FIX-002 the wrapper minted a new trace id and rebuilt
the request with `inflate_ingress`, so the audited pair carried a `packet_id`,
`trace_id`, `correlation_id` and lineage `root_id` unrelated to the packet Gate
sent. Gate_SDK holds one trace, one correlation and one root across every
generation of a request.

`ingress_packet_audit_enabled` (default `True`, hardening) registers the
wrapper in the SDK's packet-aware `(tenant, payload, packet)` form: the audited
request is the ingress packet itself and the audited response derives from it.
Engine handlers keep their `(tenant, payload)` signature, and the wire
response is unchanged.

The flag is read when handlers are registered (node app startup), because the
SDK dispatches on the wrapper's parameter count.
`INGRESS_PACKET_AUDIT_ENABLED=false` restores the reconstructed request.

---

## Querying Feature Status

Use the `feature_status` admin subaction to get current state of all gates:

```json
{
  "subaction": "feature_status"
}
```

Returns all feature flags with their current boolean/integer values.

---

## 20. Domain-neutral Cypher: Traversal Gate and Childsync (Stage 2)

**State**: Active (hardening)
**Flags**: `TRAVERSAL_GATE_EXISTS_SUBQUERY=True`, `CHILDSYNC_FOREACH_PROJECTION=True` (default on)

The CEG Stage 4 real-Neo4j proof ran two unrelated synthetic domain packs
through `handle_sync` and `handle_match` against `neo4j:5.18-enterprise` and
found three spec-driven paths that could not execute at all:

| Path | Emitted on baseline `7925c790` | Neo4j 5 response |
|---|---|---|
| `type: traversal` gate | `exists((candidate)-[:E]->(t:Label {prop: $param}))` | `PatternExpressions are not allowed to introduce new variables: 't'` |
| `childsync` endpoint | `SET n += row` with the child list still in `row` | `Property values can only be of primitive types or arrays thereof` |
| `childsync` with `edgedirection: childtoparent` | `MERGE (n)-[:T]<-(c)` | `Invalid input '<'` |

The traversal gate is the only spec-level way to constrain candidates by a
related taxonomy node's property, so without it a query field such as
`route_code` cannot constrain a match.

`traversal_gate_exists_subquery` (on): the gate compiles to
`EXISTS { MATCH (candidate)-[:E]->(t:Label) WHERE t.prop = $param }` — the form
the exclusion gate already uses — and its null handling checks the query
parameter only, because `candidateprop` on this gate type names the target
node's property, not the candidate's. Off: the legacy predicate and the legacy
`candidate.<prop>` null wrap.

`childsync_foreach_projection` (on): the parent `SET` becomes
`SET n += row{.*, <childfield>: null, …}`, children are created inside
`FOREACH (child_row IN coalesce(row.<field>, []) | …)`, `childtoparent` emits
`(n)<-[:T]-(c)`, and an undeclared direction raises. Off: the legacy
`UNWIND`-based path.

Both ship on because the legacy output cannot execute on any supported Neo4j;
the flags exist for C-21 staging and rollback, not because the legacy path is
a working alternative. Endpoints without `childsync` keep the exact previous
`SET n += row` regardless of the flag.

Two further hidden vertical assumptions surfaced by the same proof:

`gate_explicit_nullbehavior` (on): C-14 says every gate declares its null
behavior and the compiler honours it, but `GateSpec.nullbehavior` defaults to
`pass`, so an explicit `nullbehavior: pass` was indistinguishable from
"undeclared" and was overridden by the per-type default (`fail` for boolean,
freshness and traversal gates). Pydantic records which fields a spec actually
set; a declared value now wins, an undeclared one keeps the type default. This
changes ranking for packs that declared `pass` on a boolean gate and relied on
the override — PlasticOS `food_grade_check` is one. Off: the legacy override.

`spec_declared_candidate_id` (on): `handle_match` read a hardcoded `entity_id`
off every candidate for Pareto-front ids and causal explanations, a PlasticOS
convention. The id property is now the one the domain declares — the sync
endpoint's `idproperty` for the candidate label (the key the engine MERGEs on),
else the node's first `required` property — with `entity_id` as the final
fallback so existing packs are unaffected. Off: `entity_id` only.

Honouring a declared `pass` admitted PlasticOS candidates that the override had
hidden, and they exposed a third defect under the existing `score_clamp_enabled`
flag: the `[0, 1]` clamp passed a null dimension through untouched, so a
candidate missing a scored property (`point.distance` on a null `lat`, a
freshness date never synced) produced a null weighted `score` that
`handle_match` crashed ranking. Null is outside `[0, 1]`; the clamp now maps it
to `0.0`, which is the `NullStrategy.ZERO` contract the dimension strategies
already assume. The handler also ranks a null score as `0.0` when the clamp is
off. No new flag: this is the clamp doing what its flag already promises.

### Validation

Stage 4 harness (two synthetic domains, 27 proofs): 23/27 on baseline, 27/27
with both flags on. Unit coverage: `tests/unit/test_gates_all_types.py`
(`test_compile_traversal_gate*`) and `tests/unit/test_sync.py`
(`test_child_entity_sync*`) assert both the new and the flag-off shapes.

### Rollback

Set either flag to `false`. The legacy Cypher is emitted again verbatim.

---

## 21. Gate Declared Bounds (CEG-009b)

**State**: Active (hardening)
**Flag**: `GATE_DECLARED_BOUNDS_ENABLED=True` (default on)

`GateSpec` declares `queryparam_min` / `queryparam_max` (a range gate whose
candidate property must fall inside a query window) and `candidateprop_start`
/ `candidateprop_end` (a temporalrange gate whose candidate window must overlap
the query window). `GateCompiler` ignored all four, so:

| Gate as written | Emitted before | Emitted now |
|---|---|---|
| `range` with `queryparam_min: a`, `queryparam_max: b` | `candidate.min_<prop> <= $value AND $value <= candidate.max_<prop>` | `candidate.<prop> >= $a AND candidate.<prop> <= $b` |
| `temporalrange` with `candidateprop_start: s`, `candidateprop_end: e` | `candidate.timestamp >= $start AND candidate.timestamp <= $end` | `candidate.s <= $end AND candidate.e >= $start` |

`$value` is a parameter nothing supplies and `candidate.timestamp` a property
no such candidate carries, so neither legacy fragment ever matched what the
spec author wrote. `freight-matching`'s `availability_window` is the live case:
it compared `candidate.timestamp` and therefore rejected every candidate.

Off: the legacy fragments, for C-21 rollback only. The surrounding CEG-009b
change — a scalar `queryparam` (`85.0`, `true`) is bound as a generated
parameter (`$gate_<name>_value`, exposed on `GateCompiler.last_query_params`)
instead of being emitted as a parameter *name* — has no flag: the previous
output (`$85.0`, `$True`) could not execute, so there is no legacy behaviour
to return to.

### Validation

`tests/unit/test_gates_all_types.py` (`TestLiteralOperands`,
`TestDeclaredBounds`) assert both shapes and the flag-off fragments.

### Rollback

Set the flag to `false`. The placeholder fragments are emitted again verbatim.
