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
| Score Clamping | `SCORE_CLAMP_ENABLED` | `True` | `True` | active |
| Strict Null Gates | `STRICT_NULL_GATES` | `True` | `True` | active |
| Param Strict Mode | `PARAM_STRICT_MODE` | `True` | `True` | active |
| LLM Security (ValidatedLLMClient) | `LLM_PROVIDER` | `openai` (implicit) | `openai` | active — fully implemented, requires `OPENAI_API_KEY` |
| PacketStore Persistence | `PACKET_STORE_ENABLED` | `False` | `True` | active (opt-in, needs `PACKET_STORE_DSN`) |
| Outcome Persistence | `OUTCOME_PERSISTENCE_ENABLED` | `False` | `True` | active (opt-in; requires PacketStore — see §5) |
| Tenant Auth (JWT allowed_tenants) | `TENANT_AUTH_ENABLED` | `True` | `True` | active |
| Capability Auth (domain-spec model) | `CAPABILITY_AUTH_ENABLED` | `True` | `True` | active |
| PostgreSQL Audit Pool | `POSTGRES_DSN` | unset (`None`) | set | active (opt-in, soft dependency — see §7) |
| Constellation Orchestration | — | — | — | accepted architectural gap — see §9 |

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
| PacketStore | `PACKET_STORE_ENABLED` + `PACKET_STORE_DSN` | `engine/packet/packet_store.py` | `PacketStore` disabled — persistence calls no-op |

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
- `engine/gate_registration.py` already self-registers this node with Gate
  at startup (`register_node_with_gate()` in `GraphLifecycle.startup()`),
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

## 11. Tenant Auth (W3-01)

**State**: Active
**Flag**: `TENANT_AUTH_ENABLED=True` (default on)

Enforces the JWT `allowed_tenants` claim against the resolved tenant. Setting this to
`False` disables that check — acceptable only for single-tenant local development.

---

## 12. Capability Auth (W3-02 / W3-03)

**State**: Active
**Flag**: `CAPABILITY_AUTH_ENABLED=True` (default on)

Enforces the domain-spec capability model, mapping each action to the permissions it
requires. Disabling it removes per-action authorization while leaving tenant resolution
intact.

---

## Querying Feature Status

Use the `feature_status` admin subaction to get current state of all gates:

```json
{
  "subaction": "feature_status"
}
```

Returns all feature flags with their current boolean/integer values.
