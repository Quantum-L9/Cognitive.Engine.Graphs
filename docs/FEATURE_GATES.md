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
| Idea Portfolio Graph | `IDEA_PORTFOLIO_ENABLED` (`idea_portfolio_enabled`) | `False` | unset | dormant; opt-in IdeaOS portfolio reads/hydration |
| Graph Inference Feedback | `GRAPH_INFERENCE_FEEDBACK_ENABLED` (`graph_inference_feedback_enabled`) | `False` | unset | dormant; CEG → Gate → EIE `graph-inference-result` — see §13 |
| Domain Database Provisioning | `AUTO_CREATE_DOMAIN_DATABASE` (`auto_create_domain_database`) | `False` | unset | dormant; create the tenant domain database on first use — see §14 |
| Health API (admin health_* subactions) | `HEALTH_API_ENABLED` (`health_api_enabled`) | `False` | unset | dormant; AI-readiness assess/report surface — see §15 |
| Unvalidated Domain Packs | `UNVALIDATED_DOMAIN_PACKS_ENABLED` (`unvalidated_domain_packs_enabled`) | `False` | unset | dormant; seven packs, statically clean and awaiting the Neo4j 5 witness — see §16 |
| SDK Participation | `SDK_PARTICIPATION_ENABLED` (`sdk_participation_enabled`) | `True` | unset | active; Gate_SDK owns registration and readiness; off restores CEG's loop — see §17 |
| Gate Re-registration | `GATE_REREGISTRATION_ENABLED` (`gate_reregistration_enabled`), `GATE_REREGISTRATION_INTERVAL_SECONDS` | `True`, `300` | unset | active only while SDK participation is off — see §17 |
| Constellation Orchestration | — | — | — | accepted architectural gap — see §9 |
| Traversal Gate EXISTS Subquery | `TRAVERSAL_GATE_EXISTS_SUBQUERY` (`traversal_gate_exists_subquery`) | `True` | unset | active; traversal gates compile to an `EXISTS { … }` subquery Neo4j 5 accepts — see §20 |
| Childsync FOREACH Projection | `CHILDSYNC_FOREACH_PROJECTION` (`childsync_foreach_projection`) | `True` | unset | active; childsync writes children in `FOREACH` without copying them onto the parent — see §20 |
| Gate Declared Bounds | `GATE_DECLARED_BOUNDS_ENABLED` (`gate_declared_bounds_enabled`) | `True` | unset | active; range honours `queryparam_min`/`queryparam_max`, temporalrange honours `candidateprop_start`/`candidateprop_end` — see §21 |

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

### Rollback

Set back to `False`. Note that assess/report append to the conversion-event
store in `engine/health/health_report.py`, which is bounded
(`CONVERSION_EVENT_MAX`) and discards oldest-first.

---

## 16. Unvalidated Domain Packs (CEG-009)

**State**: Dormant
**Flag**: `UNVALIDATED_DOMAIN_PACKS_ENABLED=False` (default off)

CEG-009 moved nine domain packs out of a flat `<name>_domain_spec.yaml` shape
the loader never reads into `domains/<id>/spec.yaml`. Making them readable
exposed that seven did not compile to executable, faithful Cypher. Three root
causes, all in the spec-to-Cypher contract:

1. `type: traversal` gates written with `pattern` and `condition`, which
   `GateCompiler` did not consume. With no `edgetype` the compiler fell back
   to `RELATES_TO`, an edge no ontology declares, so the gate was a hard
   filter that matched nothing.
2. A scalar `queryparam` (`85.0`, `5`, `1`, `true`). `GateSpec` stringified it
   into a parameter *name*: `$85.0` (refused by C-009) or `$True` (a valid
   identifier nothing supplies).
3. `strictwhen` on a gate. `GateSpec` declared it and nothing under `engine/`
   consumed it, so a gate the author wrote as conditional ran as an
   unconditional hard filter.

CEG-009a-c closed all three in the compiler (traversal grammar, literal
operand binding, `strictwhen` — see `engine/gates/compiler.py`), and CEG-009d
rewrote the pack YAML the compiler could not be right about. The static half
is now clean for every one of the seven; what remains is the live half.

| Pack | Original defect | Static fix | Still pending |
|---|---|---|---|
| `executive-assistant` | `RELATES_TO` fallback + `$85.0` | `skill_match` rewritten to `edgetype: HAS_SKILL` → `Skill.name = $requiredskills` (the two-hop pattern named `(query)`, never a bound node, over an undeclared `REQUIRES` edge); `85.0` now binds | Neo4j 5 witness. `deadline_feasibility` compares `avgresponsetimehours` (float) with `deadline` (datetime) — a type mismatch the witness exercises only in its null-pass form. Scoring references `$param` |
| `repo-as-agent` | `RELATES_TO` fallback + `$5` | `code_ownership` rewritten to `edgetype: OWNS_MODULE` → `CodeModule.modulepath = $affectedfiles`; `5` now binds | Neo4j 5 witness. Scoring references `$param` and `$query.labels` |
| `roofing-company` | `RELATES_TO` fallback + `$1` + `$True` | `service_area` rewritten to `edgetype: SERVICES_ZIP` → `ZipCode.zipcode = $zipcode` (the pattern filtered on `code`, undeclared); `specialty_match` marked `invertible: true` (the candidate holds the list); `1` / `true` now bind; `emergency_availability` is conditional via `strictwhen` | Neo4j 5 witness. Scoring references `$param`, `$target_price`, `candidate.value`, `candidate.price_per_unit` |
| `aios-god-agent` | `RELATES_TO` fallback | `capability_match` rewritten to `edgetype: HAS_CAPABILITY` → `Capability.capabilityid = $requiredcapabilities` | Neo4j 5 witness. Scoring references `$param` |
| `healthcare-referral` | `$1` + `$True` | both bind; the `insurance_check` traversal step (a property map `TraversalAssembler` forbids, which failed every match before any gate ran) is now the `insurance_accepted` traversal gate; the `specialty_match` step over an undeclared `Condition`/`REQUIRES` is removed | Neo4j 5 witness. Scoring `geodecay` references `$lat`/`$lon` and `candidate.lat`/`candidate.lon`, none declared |
| `legal-discovery` | `strictwhen` on a gate | consumed: `privilege_filter` filters only while `$includeprivileged = false` | Neo4j 5 witness. Scoring `temporalproximity` reads `candidate.last_activity_date`, `touch_count_30d`, `is_accelerating`, none declared |
| `research-agent` | `strictwhen` on two gates | consumed: `code_availability` / `dataset_availability` filter only while their `requires*` flag is true | Neo4j 5 witness. Same `temporalproximity` property references as above |

The loader keeps two ledgers behind this flag (`engine/config/loader.py`):

- `WITNESS_PENDING_PACKS` — statically clean; waiting on the per-pack Neo4j 5
  witness, `tests/integration/test_domain_packs_neo4j.py`. The witness seeds a
  passing candidate and one failing candidate per gate, runs the compiled
  WHERE clause against `neo4j:5.18-enterprise`, and then runs the whole
  `handle_match` pipeline. **A pack leaves this ledger, and the flag, only
  when both halves are green for it.** The witness has not yet run: the
  change that wrote it had no Docker daemon, so no pack is proven and none is
  un-gated. The "still pending" column lists what the full-pipeline half is
  expected to surface — scoring-side references no query schema supplies —
  which belongs to the scoring assembler and the packs' `scoring:` sections,
  not to the gate compiler.
- `STATIC_DEFECT_PACKS` — a defect the static check still reports, with its
  reason written beside the pack. Empty at the time of writing: the new
  identifier check found `mortgage-brokerage`'s `loanamount_range` (a
  discoverable pack) referencing `$minloanamountusd` / `$maxloanamountusd`,
  which no query-schema field supplies — hidden until §21 stopped the range
  compiler emitting the placeholder `$value` — and the gate is rewritten to
  the candidate-window form (`candidateprop_min` / `candidateprop_max` on the
  `LoanProduct`, `queryparam: loanamountusd`), so the pack stays discoverable.
  Its `lenderresolve` / `lenderstatelicensing` traversal steps still fail
  `TraversalAssembler` validation (unparenthesised nodes; a property map with
  a parameter), which predates this change and is outside the gate compiler.

**Do not turn this on to "see if they work".** Static cleanliness is
necessary, not sufficient. `tests/unit/test_domain_pack_shape.py` holds each
ledger to its reason: a discoverable pack must compile to executable Cypher
referencing only parameters something supplies, a witness-pending pack must
stay statically clean, and a static-defect pack that starts compiling cleanly
must be moved on — so a pack can only move forward and never get forgotten
behind the flag.

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
