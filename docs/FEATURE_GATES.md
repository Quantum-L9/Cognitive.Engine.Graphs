<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [feature-gates, activation, runbook]
owner: engine-team
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
| Native Domain Approval Receipts | `DOMAIN_PACK_RECEIPTS_ENABLED` | `False` | unchanged | dormant; immutable native revision/receipt admission, no warm-cache fallback; see the native publisher runbook, `docs/runbooks/domain-pack-publish.md` |
| Production Domain Stage | `DOMAIN_PRODUCTION_STAGE_REQUIRED` | `False` | unchanged | dormant; production legacy admission requires reviewed/production; publisher/receipt mode always enforce stage |
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
| Boot Cancellation Propagates | `BOOT_CANCELLATION_PROPAGATES` (`boot_cancellation_propagates`) | `True` | unset | active (hardening); the compliance flush task ends cancelled and a cancelled `shutdown()` is not swallowed (SonarCloud python:S7497); off restores the previous swallowing |
| Dormant Domain Packs | `DORMANT_DOMAIN_PACKS_ENABLED` (`dormant_domain_packs_enabled`) | `False` | unset | dormant; serve packs that declare `domain.activation: dormant` (replaces the per-domain `idea_portfolio_enabled`) — see §16 |
| Graph Inference Feedback | `GRAPH_INFERENCE_FEEDBACK_ENABLED` (`graph_inference_feedback_enabled`) | `False` | unset | dormant; CEG → Gate → EIE `graph-inference-result` — see §13 |
| Relationship Evidence Sync | `RELATIONSHIP_SYNC_ENABLED` (`relationship_sync_enabled`) | `True` | unset | active; `sync` to a `targetedge` endpoint writes typed evidence edges; requires Neo4j Enterprise 5.7+ — see §28 |
| Relationship Sync Audit On Commit | `RELATIONSHIP_SYNC_AUDIT_ON_COMMIT_ENABLED` (`relationship_sync_audit_on_commit_enabled`) | `True` | unset | active (hardening); refused/rolled-back batches leave no success audit record — see §28 |
| Observed Association Required | `OBSERVED_ASSOCIATION_REQUIRED` (`observed_association_required`) | `False` | unset | staged (D-9, #386); a present `observed_association` is always checked; on, an observed row on an `observedfrom` endpoint must carry one — see §28 |
| Evidence Identity Per Type | `EVIDENCE_IDENTITY_PER_TYPE_ENABLED` (`evidence_identity_per_type_enabled`) | `False` | unset | dormant; score dedupes on `(relationship_type, evidence_ref)` — see §28 |
| Domain Database Provisioning | `AUTO_CREATE_DOMAIN_DATABASE` (`auto_create_domain_database`) | `False` | unset | dormant; create the tenant domain database on first use — see §14 |
| Health API (admin health_* subactions) | `HEALTH_API_ENABLED` (`health_api_enabled`) | `False` | unset | dormant; AI-readiness assess/report surface — see §15 |
| Unvalidated Domain Packs | `UNVALIDATED_DOMAIN_PACKS_ENABLED` (`unvalidated_domain_packs_enabled`) | `False` | unset | dormant; seven packs whose gates do not compile to executable, faithful Cypher — see §16 |
| SDK Participation | `SDK_PARTICIPATION_ENABLED` (`sdk_participation_enabled`) | `True` | unset | active; Gate_SDK owns registration and readiness; off restores CEG's loop — see §17 |
| Gate Re-registration | `GATE_REREGISTRATION_ENABLED` (`gate_reregistration_enabled`), `GATE_REREGISTRATION_INTERVAL_SECONDS` | `True`, `300` | unset | active only while SDK participation is off — see §17 |
| Causal Rejection Enforcement | `CAUSAL_REJECTION_ENFORCEMENT_ENABLED` (`causal_rejection_enforcement_enabled`) | `True` | unset | active; an R3-rejected RESULTED_IN edge is not written — see §18 |
| Ingress Packet Audit | `INGRESS_PACKET_AUDIT_ENABLED` (`ingress_packet_audit_enabled`) | `True` | unset | active; SDK audit pairs record Gate's ingress packet — see §19 |
| Constellation Orchestration | — | — | — | accepted architectural gap — see §9 |
| Gate Explicit Null Behavior | `GATE_EXPLICIT_NULLBEHAVIOR` (`gate_explicit_nullbehavior`) | `True` | unset | active; a gate's declared `nullbehavior` wins over the per-type default (C-14) — see §20 |
| Spec-declared Candidate Id | `SPEC_DECLARED_CANDIDATE_ID` (`spec_declared_candidate_id`) | `True` | unset | active; `handle_match` identifies candidates by the domain's declared id property, not `entity_id`; since RU-002 the outcome edges, legacy `resolve`, `enrich` by ids and admin `evaluate` do too — see §20 |
| Traversal Gate EXISTS Subquery | `TRAVERSAL_GATE_EXISTS_SUBQUERY` (`traversal_gate_exists_subquery`) | `True` | unset | active; traversal gates compile to an `EXISTS { … }` subquery Neo4j 5 accepts — see §20 |
| Childsync FOREACH Projection | `CHILDSYNC_FOREACH_PROJECTION` (`childsync_foreach_projection`) | `True` | unset | active; childsync writes children in `FOREACH` without copying them onto the parent — see §20 |
| Sync Declared Projection | `SYNC_DECLARED_PROJECTION_ENABLED` (`sync_declared_projection_enabled`) | `True` | unset | active (restores "the domain spec is the single source of truth"); node sync writes only the target node's declared, storable properties (`SET n += row.node`), each childsync child only what its item map and node declare (a `static` child on create only), a `list<scalar>` taxonomy field links one node per element; off restores `row{.*}` — see §31 |
| Sync Row Type Validation | `SYNC_ROW_TYPE_VALIDATION_ENABLED` (`sync_row_type_validation_enabled`) | `True` | unset | active; every node-sync row is validated against the spec's declared types before compliance logging or Cypher; any defect refuses the batch (`SyncRowRejectedError`, nothing written) listing every `row <i> <field>: expected <declared>, got <actual>` — see §31 |
| Sync Undeclared Property Refusal | `SYNC_UNDECLARED_PROPERTY_REFUSAL_ENABLED` (`sync_undeclared_property_refusal_enabled`) | `False` | unset | staged; off, undeclared row keys are dropped by the projection and counted in `undeclared_properties`; on, they are row defects that refuse the batch — see §31 |
| Match Dimension Projection | `MATCH_DIMENSION_PROJECTION` (`match_dimension_projection`) | `True` | unset | active (correctness, RU-001); match RETURNs the active dimension scores as `dimension_scores` so Pareto, arbitration and confidence checks see real values; off restores `RETURN candidate, score` |
| Gate WHERE Row Scope | `GATE_WHERE_ROW_SCOPE` (`gate_where_row_scope`) | `True` | unset | active (correctness, RU-011); match emits `WITH *` before the gate `WHERE` so gates remove candidate rows even after a trailing `OPTIONAL MATCH`; off restores `WHERE` directly after the traversal |
| Match Collapse Traversal Rows | `MATCH_COLLAPSE_TRAVERSAL_ROWS` (`match_collapse_traversal_rows`) | `True` | unset | active (correctness, RU-036); match keeps each candidate's best-scoring traversal row before `ORDER BY … LIMIT $top_n`, so `top_n` counts distinct candidates; off restores one row per path |
| Monoculture Weighted Contribution | `MONOCULTURE_WEIGHTED_CONTRIBUTION` (`monoculture_weighted_contribution`) | `True` | unset | active (correctness, RU-032); the monoculture confidence check measures `weight × value` per dimension; dimensional agreement and the returned `dimension_scores` stay raw; off restores raw-value monoculture |
| Query Defaults Before Derived | `QUERY_DEFAULTS_BEFORE_DERIVED` (`query_defaults_before_derived`) | `True` | unset | active (correctness, RU-016); match applies declared `queryschema` defaults before resolving derived parameters (caller values win); off restores resolve-then-backfill |
| Match Effective Weight Validation | `MATCH_EFFECTIVE_WEIGHT_VALIDATION` (`match_effective_weight_validation`) | `True` | unset | active (correctness, RU-009; requires `SCORE_CLAMP_ENABLED`); the ≤ 1.0 ceiling is checked on the merged per-dimension weights scoring will apply, and unknown weight keys are rejected; off restores the supplied-keys-only sum |
| Resolve Requires Created Edge | `RESOLVE_REQUIRES_CREATED_EDGE` (`resolve_requires_created_edge`) | `True` | unset | active (correctness, RU-004); legacy `resolve` raises a ValidationError when no `RESOLVED_FROM` edge was created; off restores an unconditional `resolved` |
| Outcome Map JSON Encoding | `OUTCOME_MAP_JSON_ENCODING` (`outcome_map_json_encoding`) | `True` | unset | active (correctness, RU-005); `outcomes` stores `fingerprint.dimension_weights` as sorted-key JSON that counterfactual generation decodes (legacy repr strings read as empty, never eval'd); off restores the `str(dict)` writer |
| Health Probe Domain Count Only | `HEALTH_PROBE_DOMAIN_COUNT_ONLY` (`health_probe_domain_count_only`) | `True` | unset | active (hardening, RU-013); `health` for a tenant without a pack reports `<N> domain packs`, not the loaded domain ids; off restores the id list |
| Sync Count From Write Result | `SYNC_COUNT_FROM_WRITE_RESULT` (`sync_count_from_write_result`) | `True` | unset | active (correctness, RU-060); `UNWINDMATCHSET` sync returns `count(n) AS synced_count` and the response adds `unmatched_count`; `UNWINDMERGE` unchanged; off restores `synced_count = len(batch)` |
| Admin Tenant-bound Domain | `ADMIN_TENANT_BOUND_DOMAIN` (`admin_tenant_bound_domain`) | `True` | unset | active (hardening, RU-003); every domain-bearing admin subaction rejects a `domain_id` other than the packet tenant before any load or query; `evaluate` matches as the tenant; off restores the caller-chosen `domain_id` |
| Pricealignment Float Division | `PRICEALIGNMENT_FLOAT_DIVISION` (`pricealignment_float_division`) | `True` | unset | active (correctness); both ratio operands are cast with `toFloat()` so INTEGER-stored prices are not truncated (450/500 scored 0.0); `false` restores the legacy expression (RU-017 / CEG-COR-006) |
| Arbitration Zero Score Present | `ARBITRATION_ZERO_SCORE_PRESENT` (`arbitration_zero_score_present`) | `True` | unset | active (correctness); a dimension value of 0.0 is evaluated by hard/soft constraints and Pareto dominance instead of being skipped or replaced by `<d>_score`; `false` restores legacy truthiness (RU-018 / CEG-COR-025) |
| Arbitration Objective Direction | `ARBITRATION_OBJECTIVE_DIRECTION` (`arbitration_objective_direction`) | `True` | unset | active (correctness); the arbitration front honours `direction: minimize` (unknown strings maximise with a warning) and keeps at most `front_size_limit` ids, highest composite `score` first, reporting the rest as `trimmed` (`pareto.arbitration.trimmed_ids` in the match response); a missing or non-numeric minimised value ranks as the worst cost (`inf`), not `0.0`; `false` restores maximise-all, unbounded (RU-019 / CEG-COR-026) |
| Learned Weights Tenant Keyed | `LEARNED_WEIGHTS_TENANT_KEYED` (`learned_weights_tenant_keyed`) | `True` | unset | active (correctness) for feedback-enabled domains only (`FEEDBACK_ENABLED` defaults `False`); learned DimensionWeight multipliers are read by the writer's `{dimension, tenant}` keys; `false` restores the inert legacy reader (RU-022 / CEG-COR-030) |
| Learned Weight Renormalization | `LEARNED_WEIGHT_RENORMALIZATION` (`learned_weight_renormalization`) | `True` | unset | active (correctness) for feedback-enabled domains; learning redistributes weight among non-penalised dimensions without raising their sum above the pre-learning (C-22) value, and a learned `0.0` is honoured; `false` restores multiply-only (RU-023 / CEG-COR-083) |
| Signal Weight Proportion CI | `SIGNAL_WEIGHT_PROPORTION_CI` (`signal_weight_proportion_ci`) | `True` | unset | active (correctness) for feedback-enabled domains; stored DimensionWeight `ci_width`/`confidence` come from the win-rate proportion, so an early positive lift is dampened; `false` restores the lift-based formula (RU-027 / CEG-COR-116) |
| Propagation Single Application | `PROPAGATION_SINGLE_APPLICATION` (`propagation_single_application`) | `True` | unset | active (correctness); one outcome event updates each tenant DimensionWeight `boost_factor` once, bounded, and `candidates_affected` counts DimensionWeights; `false` restores the row-multiplying query (RU-028 / CEG-COR-117) |
| Drift Requires Both Windows | `DRIFT_REQUIRES_BOTH_WINDOWS` (`drift_requires_both_windows`) | `True` | unset | active (correctness); outcomes `metadata.drift` raises no alert unless both a recent and a baseline distribution exist; `false` restores the one-sided divergence (RU-029 / CEG-COR-118) |
| Scoring Admission Check | `SCORING_ADMISSION_CHECK_ENABLED` (`scoring_admission_check_enabled`) | `False` | unset | dormant (owner decision 2026-10-29: packs keep being served as on main until the operator enables withholding); when on, admission withholds a pack whose scoring refuses to compile, references an unbound `$parameter`, or declares an HGKR option with no producer — see §16 |
| Full-match Admission | `ADMISSION_CHECKS_FULL_MATCH` (`admission_checks_full_match`) | `False` | unset | dormant (owner decision 2026-10-29: packs keep being served as on main until the operator enables withholding); when on, admission requires every declared direction's traversal to assemble and every `$parameter` in gates, traversal and scoring to be bound — see §16 |
| Exclusion Endpoint Validation | `EXCLUSION_ENDPOINT_VALIDATION` (`exclusion_endpoint_validation`) | `False` | unset | dormant (owner decision 2026-10-29: packs keep being served as on main until the operator enables withholding); when on, an exclusion gate declaring `fromnode`/`tonode`, or whose edge does not start at a candidate label of its direction, is refused instead of compiling to an always-true `NOT EXISTS` — see §16 |
| Gate Null Operands From Predicate | `GATE_NULL_OPERANDS_FROM_PREDICATE` (`gate_null_operands_from_predicate`) | `True` | unset | active; a gate's `nullbehavior` applies to exactly the operands its predicate tests (range FAIL rejects NULL bounds; temporalrange null-handles its window per side) — see §21 |
| Composite Subgate Scoping | `COMPOSITE_SUBGATE_SCOPING` (`composite_subgate_scoping`) | `True` | unset | active; a composite's subgates contribute only through the composite (an OR composite is no longer AND-ed with its own subgates) and a subgate scoped to another direction is refused — see §21 |
| Freshness Max-age Validated | `FRESHNESS_MAXAGEDAYS_VALIDATED` (`freshness_maxagedays_validated`) | `True` | unset | active (hardening); a freshness gate defaults `maxagedays` to 1 only when absent and refuses a value < 1 (0 became 1 day, a negative age a future cutoff) — see §21 |
| Prohibited Factor Full Coverage | `PROHIBITED_FACTOR_FULL_COVERAGE` (`prohibited_factor_full_coverage`) | `True` | unset | active (hardening); every gate property field, scoring dimension and sync-row key at any depth is checked, casefolded — see lane D |
| PII Redaction Recursive | `PII_REDACTION_RECURSIVE` (`pii_redaction_recursive`) | `True` | unset | active (hardening); PII detection/redaction/masking reaches list elements (match `candidates`) without mutating the input — see lane D |
| Compliance Audit Soft Sink | `COMPLIANCE_AUDIT_SOFT_SINK` (`compliance_audit_soft_sink`) | `True` | unset | active (hardening); bounded audit buffer, JSON-encoded JSONB metadata, request never fails on audit persistence — see lane D |
| Prohibited Factor Violation Audit | `PROHIBITED_FACTOR_VIOLATION_AUDIT` (`prohibited_factor_violation_audit`) | `True` | unset | active; a blocked attempt leaves one `denied` audit entry before the error propagates (domain `auditonviolation`) — see lane D |
| PII Declared Fields Enforced | `PII_DECLARED_FIELDS_ENFORCED` (`pii_declared_fields_enforced`) | `True` | unset | active; the domain's declared `compliance.pii` fields and `handling` mode apply on the response path — see lane D |
| Causal Attribution Unique Touchpoints | `CAUSAL_ATTRIBUTION_UNIQUE_TOUCHPOINTS` (`causal_attribution_unique_touchpoints`) | `True` | unset | active; attribution weights are computed over unique touchpoints and sum to 1.0 — see lane D |
| Causal Edge Declared Identity | `CAUSAL_EDGE_DECLARED_IDENTITY` (`causal_edge_declared_identity`) | `True` | unset | active; causal edges match declared node identities, anchor predicates in one WHERE with a bound threshold, MERGE on a stable key — see lane D |
| Causal Validator Batch Edge Type | `CAUSAL_VALIDATOR_BATCH_EDGE_TYPE` (`causal_validator_batch_edge_type`) | `True` | unset | active; declared per-edge temporal validation applies to sync batches — see lane D |
| safe_eval Rejects Keywords | `SAFE_EVAL_REJECT_KEYWORDS` (`safe_eval_reject_keywords`) | `True` | unset | active; keyword arguments in derived-parameter / unit expressions raise instead of being dropped — see lane D |
| Causal Serializer Variable Depth | `CAUSAL_SERIALIZER_VARIABLE_DEPTH` (`causal_serializer_variable_depth`) | `True` | unset | active; causal match explanations follow chains up to the requested depth — see lane D |
| Counterfactual Dedupe and Spec Label | `COUNTERFACTUAL_DEDUPE_AND_SPEC_LABEL` (`counterfactual_dedupe_and_spec_label`) | `True` | unset | active; no duplicate counterfactual scenarios, fingerprints read from the domain's outcome label — see lane D |
| Outcome Vocabulary Required | `OUTCOME_VOCABULARY_REQUIRED_ENABLED` (`outcome_vocabulary_required_enabled`) | `True` | unset | active (hardening, Stage 2 follow-up); `feedbackloop.outcome_node_label` has no engine default, and an outcome write or read on a pack that declares none raises `OutcomeVocabularyUndeclaredError`; every served pack declares its label — see §32 |
| PacketStore Packet-Identity Dedup | `PACKET_STORE_PACKET_IDENTITY_DEDUP_ENABLED` (`packet_store_packet_identity_dedup_enabled`) | `True` | unset | active (hardening, RU-078); `packet_store.content_hash` is the SDK `transport_hash`, so distinct packets with equal payloads are both stored, and a packet whose INSERT is skipped as a duplicate writes no hop/delegation/lineage rows; off restores the `payload_hash` key and unconditional child rows — see §7 |
| PacketStore RLS Tenant Session | `PACKET_STORE_RLS_TENANT_SESSION_ENABLED` (`packet_store_rls_tenant_session_enabled`) | `True` | unset | active (hardening, RU-079); `persist` and `record_outcome` set the transaction-local `app.current_tenant` GUC to the packet's / call's tenant before writing, so an `l9_product` DSN satisfies the DDL's RLS policies; off issues no `set_config` — see §7 |
| Inference Geography USPS Zones | `INFERENCE_GEO_USPS_ZONES_ENABLED` (`inference_geo_usps_zones_enabled`) | `True` | unset | active (correctness, RU-084); `infer_geography_from_postal_code` maps the ZIP first digit to its USPS zone (0-1 Northeast, 2-3 Southeast, 4-6 Midwest, 7 South, 8-9 West) and emits nothing when the entity declares a non-US `country`; off restores the five-digit threshold ladder. The rule also needs `graph_inference_feedback_enabled` and domain authorization — see §13 |
| Inference Persona Token Match | `INFERENCE_PERSONA_TOKEN_MATCH_ENABLED` (`inference_persona_token_match_enabled`) | `True` | unset | active (correctness, RU-088); `infer_buyer_persona` matches a keyword only as a whole job-title token (split on `[^a-z0-9&]+`), so "Marketing Coordinator" is no longer executive; keyword sets, precedence and confidences unchanged; off restores substring matching — see §13 |
| Inference Input Parsing Strict | `INFERENCE_INPUT_PARSING_STRICT_ENABLED` (`inference_input_parsing_strict_enabled`) | `True` | unset | active (correctness, RU-089); `infer_company_size_tier` falls back to `employees`/`revenue` only when the primary field is absent and parses each value separately (an unparseable employee count falls back to revenue), and `infer_email_domain_from_website` emits the URL hostname without port, userinfo or `www.`; off restores the legacy parsing — see §13 |
| Packet Audit on Cancel | `PACKET_AUDIT_ON_CANCEL_ENABLED` (`packet_audit_on_cancel_enabled`) | `True` | unset | active (correctness, RU-086); a handler the SDK cancels on timeout persists one failed request/response pair (`engine_data={"error": "handler_cancelled"}`) under `asyncio.shield` before the cancellation is re-raised; off records no pair for a cancelled handler — see §19 |
| Capability Record Binding | `CAPABILITY_RECORD_BINDING_ENABLED` (`capability_record_binding_enabled`) | `True` | unset | active (hardening, RU-090); `CapabilityValidator.validate_action` authorizes only an object equal to the registered record (incl. `proof_hash`) and reads revocation, expiry, tenant and actions from that record; `expires_at` is part of `proof_hash`, so the admin `delegate_capability` response carries a different `proof_hash` value; off restores the legacy checks and hash input — see §12 |
| Health Absent Confidence Counts | `HEALTH_ABSENT_CONFIDENCE_COUNTS_ENABLED` (`health_absent_confidence_counts_enabled`) | `True` | unset | active (hardening); an absent field confidence is "no signal", only an explicit confidence < 0.70 discounts a populated field; batch/report forward `field_confidences` (RU-108 / CEG-COR-044) — see §15 |
| Health Range Gate Fields | `HEALTH_RANGE_GATE_FIELDS_ENABLED` (`health_range_gate_fields_enabled`) | `True` | unset | active (hardening); range/window bound properties (`candidateprop_min/max/start/end`) are gate-critical in health readiness (RU-109 / CEG-COR-045) — see §15 |
| Health Blocked Score Scale | `HEALTH_BLOCKED_SCORE_SCALE_ENABLED` (`health_blocked_score_scale_enabled`) | `True` | unset | active (hardening); a blocked readiness reports the 60% gate component `round(gc*60, 2)` so the score is monotonic in gate completeness (RU-110 / CEG-COR-046) — see §15 |
| Health Priority Staleness | `HEALTH_PRIORITY_STALENESS_ENABLED` (`health_priority_staleness_enabled`) | `True` | unset | active (hardening); the 10% staleness term of the enrichment priority is computed from field staleness instead of always 0 (RU-111 / CEG-COR-047) — see §15 |
| Legacy Chassis Auth | `LEGACY_CHASSIS_AUTH_ENABLED` (`legacy_chassis_auth_enabled`) | `True` | unset | active (hardening, dev/test-only legacy chassis); with `L9_API_KEY` set, `chassis_app.create_app` registers `BearerAuthMiddleware` (`/v1/health` public) and POST `/v1/execute` returns 403 for a tenant outside the token's `allowed_tenants` while `TENANT_AUTH_ENABLED`; the factory also refuses to build outside dev/local/test while `REQUIRE_SDK_CHASSIS_IN_PROD` (RU-082 / CEG-COR-039); off restores the unauthenticated factory — see §11 |
| Legacy Chassis Status Mapping | `LEGACY_CHASSIS_STATUS_MAPPING_ENABLED` (`legacy_chassis_status_mapping_enabled`) | `True` | unset | active (correctness, dev/test-only legacy chassis); `chassis.actions.execute_action` attaches an HTTP status hint that POST `/v1/execute` honours: circuit open → 503 + `Retry-After`, `ChassisError` (e.g. `FeatureNotEnabled`) → its `status_code`, engine `ValidationError` → 422, other `EngineError` → 500 with its message, anything else → 500 `Internal server error` (the exception text stays in logs and the persisted response packet) (RU-083 / CEG-COR-087 / CEG-COR-088); off restores 200 for circuit open and the keyword mapping with the raw exception text — see §11 |

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
- Live erasure requires the `POSTGRES_DSN` compliance audit pool: the pre-delete
  `PII_ERASURE` flush is strict, so without it nothing is deleted (RU-066 follow-up).

### Activation Steps
1. Set `GDPR_ERASURE_ENABLED=True` in environment.
2. Keep `GDPR_DRY_RUN=True` initially for safe validation.
3. Call `erase_subject` admin subaction with `data_subject_id`.
4. Review dry-run report: `would_affect.graph_nodes` and `would_affect.packets` (the
   requesting tenant's PacketStore rows; `null` while PacketStore is disabled).
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
   hand). `record_outcome()` needs the `packet_store.outcome_jsonb` column,
   which the file declares (`ADD COLUMN IF NOT EXISTS`, RU-081); a database
   created from an older version of the file gets it by re-applying
   `packet_store.sql` (safe since RU-080, see Schema below). There is no
   separate migration: the `0001_outcome_jsonb` migration this step used to
   name never existed.
2. `PACKET_STORE_DSN` set to that instance with a role that can `INSERT` into
   `packet_store`, `lineage_graph`, `hop_trace`, `delegation_chain` and
   `UPDATE packet_store.outcome_jsonb`. A login role that is a member of
   `l9_product` has exactly that: the file grants `UPDATE (outcome_jsonb)` at
   column level, so the envelope and every other packet column stay
   non-updatable, and the `tenant_isolation_update_outcome` RLS policy limits
   the update to rows whose `actor_tenant` is the transaction's
   `app.current_tenant` (set by `PacketStore` since RU-079). Before RU-081 the
   column, the grant and the policy were all absent, so `record_outcome()`
   always returned `False`. The DSN is a secret: provide it from the
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

**Production provisioning (approved 2026-11-08).** `docker-compose.prod.yml` now
runs a `postgres` service with `packet_store.sql` applied on first init, and sets
the API's `POSTGRES_DSN` to the `l9_product` app role created by
`scripts/postgres-init/02_ceg_app_role.sh`. That satisfies prerequisites 1 and 3
for the compliance audit flush, which is now on in production. PacketStore is
on as well (approved the same day): `PACKET_STORE_ENABLED=true` and
`PACKET_STORE_DSN` with the same `l9_product` app role (prerequisites 2 and 4),
and `OUTCOME_PERSISTENCE_ENABLED=true` (prerequisite 5, §10). Verified as that
role: a packet pair persisted with lineage, a re-delivery was deduplicated,
another tenant saw no rows, `record_outcome()` succeeded, and an envelope
`UPDATE` was refused. No TTL pruning is scheduled (`prune_expired_packets()` has
no caller). Guards: `tests/architecture/test_prod_postgres_audit_store.py`.

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

The file is safe to re-apply (RU-080 / CEG-COR-096): it drops no table, every
`CREATE TABLE` / `CREATE INDEX` is `IF NOT EXISTS`, each policy is dropped and
re-created with the same definition, roles are created only when absent, and
functions are `CREATE OR REPLACE`. The whole script runs in one transaction,
so a failure leaves the database as it was. Re-apply it with
`psql -v ON_ERROR_STOP=1 -f engine/packet/packet_store.sql` to bring an existing
database up to the shipped schema; existing rows, including the append-only
`packet_audit_log`, are kept. Before RU-080 the file began with
`DROP TABLE ... CASCADE` of `packet_store`, `lineage_graph` and
`packet_audit_log`, so a re-apply deleted every packet and audit row and then
aborted on `CREATE TABLE hop_trace`, leaving `hop_trace` / `delegation_chain`
without their foreign keys and the `hop_tenant` policy. A database damaged that
way is not repaired by re-applying the file; it needs an operator-run repair.

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

**State**: Dormant by default; on in `docker-compose.prod.yml` (2026-11-08)
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
than keep code that documented an enforcement which did not exist.

**Legacy chassis (RU-082 / CEG-COR-039, DEC-29/DEC-30).** The legacy dict chassis
(`chassis/chassis_app.py`, dev/test only) now enforces the claim at the chassis,
where it belongs:

- `create_app` refuses to build (`ValueError`) outside `dev`/`local`/`test` while
  `REQUIRE_SDK_CHASSIS_IN_PROD` is on — the same predicate as
  `chassis.entrypoint.resolve_chassis` (`legacy_chassis_refusal`), so
  `uvicorn chassis.chassis_app:create_app --factory` cannot bypass the entrypoint.
- With `L9_API_KEY` set (`ChassisSettings.l9_api_key`) and
  `LEGACY_CHASSIS_AUTH_ENABLED=True` (default), `BearerAuthMiddleware` is
  registered: no `Authorization` → 401, wrong key → 403, `/v1/health` public.
- With `TENANT_AUTH_ENABLED`, POST `/v1/execute` compares `ExecuteRequest.tenant`
  with the token's `allowed_tenants` and returns 403 on a mismatch; a valid
  `X-Internal-Bypass-Key` skips the comparison. A string claim names one tenant.
- Without `L9_API_KEY` the legacy chassis stays auth-free, which is acceptable
  only because it cannot start outside dev/local/test.

Rollback in dev: `LEGACY_CHASSIS_AUTH_ENABLED=false` and/or
`TENANT_AUTH_ENABLED=false`; the factory guard is relaxed only by
`REQUIRE_SDK_CHASSIS_IN_PROD=false`. The SDK chassis (`chassis/node_app.py`) is
unaffected.

**Legacy chassis status mapping (RU-083 / CEG-COR-087 / CEG-COR-088).** With
`LEGACY_CHASSIS_STATUS_MAPPING_ENABLED=True` (default), the legacy POST
`/v1/execute` keeps the failure class: an open Neo4j circuit is HTTP 503 with a
`Retry-After` header (seconds, rounded up) instead of 200 with `status_code: 503`
inside `data`; `FeatureNotEnabled` and other `ChassisError`s use their
`status_code` (422 for a disabled feature) and an engine `ValidationError` (e.g.
the tenant allowlist rejection) is 422, where both were 500; other `EngineError`s
are 500 with their message; any other exception is 500 with the detail
`Internal server error` — before, its raw text (connection URIs, query text)
was the response body. The persisted response packet is unchanged and still
carries the error text. An unknown action stays 400 and success responses are
unchanged. Off restores the previous mapping.

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
requires. In deployed environments, a domain policy must explicitly grant every write
capability (`sync:write` includes `resolve`, `admin:write` includes admin operations).
An absent or missing policy entry never grants a graph write in production or other
deployed environments. In `dev`, `local`, and `test`, the capability guard retains
the established legacy compatibility bypass. `CAPABILITY_AUTH_ENABLED=False` is also
permitted only in those environments; production and staging-style environments reject
both bypasses.

To enable a write action in production, add the corresponding capability and allowed
subject to the domain pack, deploy the pack, and verify a denied read-only request before
admitting traffic. This CEG mechanism does not replace Gate/SDK caller authentication or
route authorization.

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

Exposes `engine/health/api.py` through the subactions `health_assess`,
`health_batch_assess` and `health_report`. That module implemented three
handlers and was imported by nothing, so `request_enrichment` — the whole
CEG → Gate → EIE direction — had no trigger an inbound packet could reach.

`AUTO_ENRICH_VIA_GATE` is not a substitute: it gates only the eventual outbound
Gate request, not assessment, reporting, or conversion-event tracking. The
surface needs a gate of its own, which is what this is.

### Route (HEALTH-01)

Health is **not** routed through `admin` (operator decision 2026-10-22). The
canonical route is the advertised action `health-assess`, owned by `ceg` in
Gate's action-ownership map (Constellation.Gate lane G3: consumer and operator
keys call it, worker keys are refused it). `engine/handlers.py::handle_health_assess`
dispatches exactly the three subactions above and refuses every other
`subaction` with `ValidationError`, so nothing else `admin` offers is reachable
from a Gate-routed packet. `admin`, `health` and `healthcheck` are never
advertised (`engine/spec.yaml`, `tests/architecture/test_seam_gate_only.py`);
the same subactions stay on `admin` only so a direct operator call in dev keeps
working. Both routes share one dispatcher (`_dispatch_health`), so the flag,
the HEALTH-05 check and the results are identical.

The SDK runtime admits inbound actions from `L9_ALLOWED_ACTIONS`
(`constellation_node_sdk.runtime.inbound_policy`). `make dev` derives that list
from `ACTION_HANDLERS`; the deployment surfaces (`docker-compose.yml`,
`docker-compose.prod.yml`, `.env.template`) enumerate it by hand and must list
`health-assess` for a Gate-routed health packet to be admitted.

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

### Scoring admission (RU-020 / CEG-COR-027..029)

`SCORING_ADMISSION_CHECK_ENABLED` (default `False` — owner decision 2026-10-29:
withholding stays off, and every pack is served as on main, until the operator
enables it) extends `admission_defects`
from gates to scoring. For every declared match direction each applicable
dimension is compiled as `ScoringAssembler` compiles it, and the pack is
withheld when:

- the scoring compiler refuses a dimension (`preference_attention` without
  `metadata.success_value`, which the schema cannot declare);
- a dimension references a `$parameter` that `handle_match` does not bind —
  not a queryschema field, derived parameter, scoring `queryprop`, `top_n`, or
  a value the assembler binds itself (`community_bridge` without a
  `query_<prop>` field; `geodecay`'s `$lon`);
- a dimension declares an option nothing produces: `null_strategy:
  inherit_prior` or `cold_start_fallback: structural_similarity`, unless the
  pack declares the property they read (`_prior_<dim>`,
  `shared_neighbor_score`) on a candidate node, which sync writes.
  `population_mean` (either option) reads `$_popmean_<dim>`: a pack that
  declares it as a queryschema field or derived parameter is admitted
  (`handle_match` binds it); otherwise it is the unbound-parameter defect above.

Each such pack failed every match (`ValueError` / `ParameterMissing`) or
silently degraded. `freight-matching` was withheld by this check while its
scoring referenced `$lat` / `$lon` / `$target_price` (CEG-COR-011); since RU-037
its dimensions declare their operands and it is served.
Rollback: `SCORING_ADMISSION_CHECK_ENABLED=false` (gate-only admission, the
previous wording of the withholding reason), or
`UNVALIDATED_DOMAIN_PACKS_ENABLED=true` to serve every validated pack.

### Full-match admission (RU-039)

`ADMISSION_CHECKS_FULL_MATCH` (default `False` — owner decision 2026-10-29:
withholding stays off, and every pack is served as on main, until the operator
enables it) makes admission keep the promise
that an admitted pack executes for every declared match direction. For each
direction `TraversalAssembler.assemble_traversal` must not raise,
`GateCompiler.compile_all_gates` must not raise, and every `$parameter` in the
gate WHERE, the traversal and the scoring clause must be a queryschema field, a
derived parameter, a scoring `queryprop` (backfilled by `handle_match`), `top_n`
or a value a compiler binds itself; otherwise the defect names the direction
and the parameter or step. No Neo4j access. `mortgage-brokerage` was withheld
by it until RU-038 rewrote its `lenderresolve` step in the assembler's
`(alias:Label)-[:TYPE]->(alias:Label)` grammar (CEG-COR-012); it failed every
match before. The repaired pack compiles cleanly. It is served, as on main
(owner decision 2026-10-29), without state-licensing or lender-blacklist
enforcement; its DEC-15 `domain.activation: dormant` declaration is left
commented in the pack until the owner enables withholding. Rollback: `ADMISSION_CHECKS_FULL_MATCH=false` (with
`SCORING_ADMISSION_CHECK_ENABLED=false` for the pre-remediation verdicts) or
`UNVALIDATED_DOMAIN_PACKS_ENABLED=true`.

### Exclusion endpoint validation (RU-041 / CEG-COR-019)

`EXCLUSION_ENDPOINT_VALIDATION` (default `False` — owner decision 2026-10-29;
the legacy fragment below is what ships until the operator enables it): the
exclusion gate compiles
to `NOT EXISTS { MATCH (candidate)-[:EDGE]->(…) }`, always anchored at the
matched candidate. A gate that declares `fromnode` / `tonode` (an anchor the
compiler ignores), or whose ontology edge type is undeclared or does not start
at a candidate label of the gate's direction(s), compiled to a predicate that
is always true. The compiler now refuses it, naming the gate, and admission
withholds the pack. The case that motivated it, `mortgage-brokerage`
`blacklist_exclusion` (`BorrowerProfile -[:BLACKLISTEDLENDER]-> Lender`), was
removed by RU-038; until a query-anchored exclusion construct exists (DEC-15 "D
now, then A") the blacklist is not enforced, and the pack is served as on main
(owner decision 2026-10-29) rather than declared dormant. A
candidate-origin edge (PlasticOS `exclusion_check`) compiles unchanged.
Rollback: `EXCLUSION_ENDPOINT_VALIDATION=false`.

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

## 28. Relationship Evidence Sync (QM.POC0.CEG.GRAPH_EVIDENCE.002A)

**State**: Active (default on since 2026-10-30, operator decision)
**Flag**: `RELATIONSHIP_SYNC_ENABLED=True` (`relationship_sync_enabled`, default on)
**Requires**: Neo4j Enterprise Edition 5.7+ (relationship property uniqueness constraints)

`sync` to an endpoint that declares `targetedge` / `fromnode` / `tonode` writes
typed evidence edges (`ceg.relationship_evidence.v1`) that the local
`graph_evidence_priority_v1` scorer reads. Off: such a request is refused with
`FeatureNotEnabled` and no edge is written, whatever the tenant spec declares.
On: the declaration, tenant and evidence guards all still apply, the batch is
written in one transaction, and a per-type uniqueness constraint on
`(_tenant, evidence_ref)` is created on first use. Node sync is unaffected in
both states.

### Enterprise requirement (open operator action)

The `(_tenant, evidence_ref)` uniqueness constraint is what stops two
concurrent writers both committing one `evidence_ref`; a transaction alone
cannot, because each writer's MERGE matches only its own endpoint pair. The
constraint is a relationship property uniqueness constraint, an Enterprise
Edition 5.7+ feature. On Community Edition creating it fails, and every
relationship sync is refused with `ExecutionError` before any write ("requires
Neo4j Enterprise 5.7+; nothing written"). Node sync is unaffected.

**Open:** the production Neo4j account is not yet on Enterprise. Until it is
upgraded, relationship sync fails closed in production. Tracked in
`.claude/rules/system-state.md` (Pending Operator Actions).

### Validation

`tests/unit/test_relationship_sync.py` (flag off/on, node sync unchanged,
constraint unavailable refuses the batch) and
`tests/integration/test_graph_evidence_score.py` (rollback, concurrent
identity, replay).

### Tenant mapping declarations (QM.POC0.TENANT_ASSOCIATION_MAPPING.004)

`observedfrom` (provider associations a relationship endpoint accepts),
`refnamespace` (a node label's graph-ref namespace) and `evidencerelevance`
(approved relevance of an evidence edge type) are tenant-spec declarations, not
flags. They are validated on every spec load, unconditionally: a malformed,
ambiguous or conflicting declaration stops the spec from loading
(`engine/config/tenant_mapping.py`). Writes they describe are still gated by
`RELATIONSHIP_SYNC_ENABLED`. A declared `refnamespace` refuses node and
relationship rows whose ids fall outside it, before compliance records the
request or anything touches the graph. Relevance reaches scoring only from a
`-reviewed` or `-production` spec. Tests:
`tests/unit/test_observed_association_schema.py`,
`tests/integration/test_tenant_mapping_identity.py`.

### Rollback

Set the flag to `false`. Edges already written keep their stamp and stay
readable by the scorer; the constraint is left in place.

### Audit on commit (`relationship_sync_audit_on_commit_enabled`, default `True`)

The compliance checks (prohibited fields, field mappings) still run before any
graph I/O, but the `Sync <type> batch_size=N` mutation record is buffered only
after the relationship write committed. A refused or rolled-back batch records
nothing; no audit entry is ever removed. Off: recorded before the write, as for
node sync. Tests: `tests/unit/test_relationship_sync.py` (refusal, rollback).

### Observed association provenance (D-9, #386)

The tenant spec held by CEG decides which provider associations a relationship
endpoint accepts (`observedfrom`). A producer's mirror of that spec is not CEG
approval. A relationship row may carry an optional `observed_association`
object with exactly `provider`, `fromobjecttype`, `toobjecttype`,
`associationcategory` and `associationtypeid`. It is provenance, checked and
never stored, so `ceg.relationship_evidence.v1` is unchanged (additive key).

Always, whatever the flag: a present key must name one entry of the endpoint's
own `observedfrom`, on a row with `assertion_status: observed`. A malformed,
reversed or undeclared key, a key on an endpoint without `observedfrom`, and a
key on a `reported`/`inferred` row refuse the whole batch before compliance
records the request or anything touches the graph, with
`ObservedAssociationRejectedError` (a `ValidationError`, HTTP 422). A failure packet
carries the error class name, not its message, so that name is how a producer
learns its spec mirror has drifted from CEG.

`observed_association_required` (`OBSERVED_ASSOCIATION_REQUIRED`, default
`False`): on, an observed row for an endpoint that declares `observedfrom` must
carry the key. It ships off because EIE does not send the key yet, and turning
it on first would refuse every observed row EIE sends. **It must be on before
production/live acceptance.** Order: deploy this CEG change, then EIE sending
the key (Quantum-L9/Enrichment.Inference.Engine), then set the flag. Endpoints
without `observedfrom` are unchanged in both states. Tests:
`tests/unit/test_observed_association_provenance.py`.

### Evidence identity per type (`evidence_identity_per_type_enabled`, default `False`)

On: `handle_score` dedupes on `(relationship_type, evidence_ref)`, the identity
this sync writes, so one `evidence_ref` under two types is not
`conflicting_evidence`. Off: `evidence_ref` alone. Tests:
`tests/unit/test_graph_evidence_binding.py`.

## 31. Typed sync contract — list and map shapes (2026-11-11)

**Flags**: `SYNC_DECLARED_PROJECTION_ENABLED=True`, `SYNC_ROW_TYPE_VALIDATION_ENABLED=True`,
`SYNC_UNDECLARED_PROPERTY_REFUSAL_ENABLED=False`

The sync generator relied on shapes the spec could not declare: childsync
assumed a list of maps under an untyped `field`, taxonomy edges assumed a
scalar (a list silently created no edge while the sync reported success), and
`SET n += row{.*}` stored whatever a row carried. `PropertySpec` now declares
`list` (element type in `items`) and `map` (keys in `properties`):

```yaml
- {name: certifications, type: list, items: {type: string}}
- name: processed_polymers
  type: list
  items:
    type: map
    properties:
      - {name: code, type: string, required: true}
```

Storage rule: `list<primitive>` is storable; `map` and `list<map>` are wire-only
and legal on a node only as the `field` of a childsync on a sync endpoint
targeting that node. Admission (`admission_defects`, unconditional — the
override is `UNVALIDATED_DOMAIN_PACKS_ENABLED`, §16) refuses a declared `map` /
`list<map>` nothing consumes (Neo4j cannot store either), and a node sync
endpoint whose childsync field is not declared `list<map>` on `targetnode`,
whose `targetid` is not a required scalar of the item map (and a scalar of the
child node), whose taxonomy field is not a scalar or `list<scalar>`, or whose
`fieldsupdated` names an undeclared or wire-only property.

| Flag | On | Off |
|---|---|---|
| `sync_declared_projection_enabled` | `SyncGenerator.project_batch` binds `$batch` as `{node: <declared storable keys present>, children: {<field>: [<item keys the child node declares>]}}`; the Cypher is `SET n += row.node`, `FOREACH (child_row IN coalesce(row.children.<field>, []) \| MERGE ... SET c += child_row ...)` (`ON CREATE SET` for a `managedby: static` child), and a `list<scalar>` taxonomy field `OPTIONAL MATCH (tax:L) WHERE tax.<id> IN coalesce(row.node.<field>, [])` → one edge per element | `SET n += row{.*, <child fields>: null}` / `SET n += row` and the §20 paths |
| `sync_row_type_validation_enabled` | `handle_sync` builds a `SyncRowContract` (`engine/sync/row_contract.py`) for a node endpoint and refuses the whole batch on any defect, before compliance logging and before any Cypher, with `SyncRowRejectedError` (a `ValidationError`) listing every defect: `row 0 processed_polymers: expected list<map>, got string`, `row 1 processed_polymers[0]: expected map, got string`, `row 2 rate_per_ton: expected float, got string`, `row 3 processed_polymers[0].code: expected string (required), got missing`. No coercion; values are never echoed (only JSON types). `required` is enforced on the endpoint's `idproperty` and on map members; `nullable: false` refuses null | No ingress check; a mistyped row fails the whole statement inside Neo4j (`ExecutionError`) |
| `sync_undeclared_property_refusal_enabled` | Undeclared keys (top level or inside a map) are row defects: `row 0 color: expected no such declared property, got string` | Undeclared keys are dropped by the projection (never written), counted in the response as `undeclared_properties: {<key path>: <rows>}` and logged by name |

Rollback of the write and ingress behaviour is `SYNC_DECLARED_PROJECTION_ENABLED=false`
and `SYNC_ROW_TYPE_VALIDATION_ENABLED=false`; a pack whose declarations admission
refuses is served only with `UNVALIDATED_DOMAIN_PACKS_ENABLED=true`. Domain-spec
changes of this section: plasticos 1.4.0 declares `Facility.processed_polymers`
as `list<map{code: string required}>`; the synthetic `orbital-logistics` 0.2.0
fixture carries a `list<string>` property, a `list<string>` taxonomy field and a
`list<map>` childsync.

## 29. Gate-routed score and Gate key binding (SCORING-GATE-01)

### Gate signing key binding (`gate_signing_key_binding_enabled`, default `True`) — GATE-KEY-01

The SDK verifies an inbound `/v1/execute` signature against any key in
`L9_VERIFYING_KEYS_JSON`, and the chassis gate-only check compared claims the
signer chooses (`source_node`, `origin_kind`, `resolved_by_gate`). A peer whose
key CEG verifies could therefore claim Gate and hand a handler any tenant.

On: `L9_GATE_SIGNING_KEY_IDS` (comma-separated) names the key ids Gate signs
with, and a packet signed under any other id, or unsigned, is refused with 403
before the SDK runs it. In staging/prod (`L9_ENVIRONMENT` staging/prod, or
`L9_ENV=prod`) the list is required — startup fails without it — and an
`hmac-sha256` signature is refused, because every verifier of a shared HMAC key
can mint it. Gate must sign with `ed25519` there and CEG must hold only Gate's
public key under those ids. Elsewhere an unset list keeps the claim-only check
with a warning. Off: the claim-only check everywhere.

**Operator action:** set `L9_GATE_SIGNING_KEY_IDS` and move Gate to ed25519
before deploying this build to staging/prod. Tests: `tests/unit/test_node_app.py`
(GATE-KEY-01 section).

### Remote score (`remote_score_enabled`, default `False`)

`score` is registered (`handle_score_remote`) and advertised, but refused with
`FeatureNotEnabled` before any spec load or graph read while off. Production
stays off pending a separate operator approval. On, every one of these still
holds, each checked before the read:

- `evidence_identity_per_type_enabled` is on (otherwise `FeatureNotEnabled`: a
  ref-only rank is never served remotely, whatever the flag's default);
- the tenant passes the allowlist and is a legal database name;
- the request is `{entity_refs, top_n}` only (server-owned fields refused);
- the tenant spec grants `score:read` to the tenant by name. A wildcard, another
  tenant's grant, `match:read`, or no `capabilities` section is refused with
  `AuthorizationError`, independently of `CAPABILITY_AUTH_ENABLED`;
- the spec has approved `evidencerelevance` (`-reviewed`/`-production`) and at
  least one `targetedge` evidence type, otherwise the result is `blocked`.

The binding (`GraphEvidenceBinding`, type-bounded by the spec's declared types)
is built from the admitted spec and the injected driver; the local
`handle_score` and the `graph_evidence_priority_v1` algorithm are unchanged.
Activation also needs `score` in `L9_ALLOWED_ACTIONS`, a Gate that owns `score`
(Gate refuses a registration advertising an action it does not own), a Gate
consumer key policy naming `score` and the tenant, GATE-KEY-01 in force, and
Neo4j Enterprise 5.7+ for the evidence it reads. Tests:
`tests/unit/test_handle_score.py` (SCORING-GATE-01 section).

### Rollback

`REMOTE_SCORE_ENABLED=false` refuses every remote score immediately.
`GATE_SIGNING_KEY_BINDING_ENABLED=false` restores the claim-only ingress check
(not recommended: it reopens tenant injection by any verified peer).

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
spec author wrote. `freight-matching`'s `availability_window` was the live case:
it compared `candidate.timestamp` and therefore rejected every candidate (since
RU-046 it is a `threshold` gate, `availabledate <= $pickupdate`).

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

### Null operands from the predicate (RU-042 / CEG-COR-020, CEG-COR-021)

`GATE_NULL_OPERANDS_FROM_PREDICATE` (default `True`): each gate handler reports
the operands its predicate tests and `NullHandler.wrap_operands` applies the
declared policy to exactly those — `pass`: a NULL operand imposes no
constraint on its side; `fail`: any NULL tested operand rejects. Before, the
wrap always used `candidate.<candidateprop>` and `$<queryparam>`:

| Gate | Before | Now |
|---|---|---|
| `range` `fail`, candidate bounds | NULL `min_`/`max_` bound passed | NULL bound rejects (`candidate.min_x IS NOT NULL AND …`) |
| `range` with `candidateprop` and derived bounds | null-checked `candidate.<prop>`, which it never reads | only the bounds and `$<queryparam>` |
| `range` query window | a NULL `$min`/`$max` rejected under `pass` | each NULL bound is unconstrained on its side |
| `temporalrange` window forms | no null handling | per-side handling of both candidate props and both params |
| `temporalrange` with derived names | null-checked undeclared `$<queryparam>` | `$<queryparam>_start` / `_end` |
| `exclusion` with `candidateprop` | null-checked `candidate.<candidateprop>` (a target-node property) | `$<queryparam>` only |

Compiled fragments of `plasticos` and `mortgage-brokerage` are byte-identical
(they declare `pass` with explicit bounds); of the shipped packs only
temporalrange window gates change (dormant `legal-discovery`
`temporal_relevance`; `freight-matching` `availability_window` until RU-046
re-expressed it as a threshold gate). Rollback:
`GATE_NULL_OPERANDS_FROM_PREDICATE=false`.

### Composite subgate scoping (RU-043 / CEG-COR-022)

`COMPOSITE_SUBGATE_SCOPING` (default `True`): `compile_all_gates` and
`compile_relaxed` skip, at top level, every gate referenced as a subgate by a
composite applicable to the direction, so `logic: OR` over `ga`, `gb` compiles
to `((ga) OR (gb))` instead of `(ga) AND (gb) AND ((ga) OR (gb))`. A subgate
whose `matchdirections` exclude the direction being compiled is refused (it was
silently compiled inside the composite). Cycle and missing-subgate refusal
(CEG-009c) are unchanged; specs without composites compile byte-identically
(no shipped pack declares one). Rollback: `COMPOSITE_SUBGATE_SCOPING=false`.

### Freshness max-age validation (RU-052 / CEG-COR-079)

`FRESHNESS_MAXAGEDAYS_VALIDATED` (default `True`): a freshness gate without
`maxagedays` still compiles as 1 day; `maxagedays: 0` (formerly coerced to 1)
and negative values (formerly a cutoff in the future) raise at compile, so
admission withholds the pack naming the gate. No shipped pack is affected
(the template declares 90). Rollback: `FRESHNESS_MAXAGEDAYS_VALIDATED=false`.

---

## Correctness remediation — lane D (plan 0d84ec8)

Safety fixes from the correctness remediation plan, lane D (compliance, PII,
prohibited factors, causal, safe_eval). Each ships on and each flag restores the
previous behaviour when set to `false`.

### Prohibited Factor Full Coverage (RU-061 / CEG-COR-008)

**Flag**: `PROHIBITED_FACTOR_FULL_COVERAGE=True` (default on)

On: `ProhibitedFactorValidator` compares, casefolded, every gate property field
(`candidateprop`, `candidateprop_min/_max/_start/_end`, str `queryparam`,
`queryparam_min/_max/_start/_end`), every scoring dimension `candidateprop` /
`queryprop` (`ComplianceEngine.validate_scoring`, called by `handle_match` next to
`validate_gates`) and every key at any depth of a sync row
(`validate_sync_row`, nested dicts and lists of dicts). Off: the legacy check of
gate `candidateprop` / `queryparam` and top-level sync keys, exact-case.
Contract: `docs/contracts/PROHIBITED_FACTORS.md`.

Rollback: set the flag to `false` while a spec is corrected.

### PII Redaction Recursive (RU-062 / CEG-COR-009)

**Flag**: `PII_REDACTION_RECURSIVE=True` (default on)

`engine/compliance/pii.py` `PIIHandler.detect_pii` descended only into dicts, so the
match response's `candidates` list was opaque and `ComplianceEngine.redact_response`
returned candidate PII unchanged; `redact()` / `mask_fields()` shallow-copied and then
edited nested caller dicts in place. On: detection descends into dicts and
lists/tuples at any depth with index-aware paths (`candidates[0].candidate.contact_email`;
dict-only payloads keep their dotted paths), and redaction/masking copy every container
on the edited path, so the input is never mutated. `redact_response` reaches
`candidates` only for a domain whose `compliance.pii.enabled` is `true`; a domain that
opts out (`enabled: false`, or no `pii` section — `PIISpec.enabled` defaults to `false`)
keeps the pre-RU-062 reach: dict paths only, candidate fields untouched.
`check_sync_request` records the detected field paths (never values) in the access audit
entry. Off: the legacy dict-only traversal.

Rollback: set the flag to `false`.

### Compliance Audit Soft Sink (RU-065 / CEG-COR-052, CEG-COR-053)

**Flag**: `COMPLIANCE_AUDIT_SOFT_SINK=True` (default on)

The PostgreSQL audit pool (`POSTGRES_DSN`, §7) is a soft dependency, but without it
`AuditLogger` retained every entry forever, and with it every flush failed (the
`metadata` dict was bound to the JSONB column) and `handle_match` / `handle_sync`
re-raised the failure after their Neo4j work had succeeded. On:

- `ComplianceEngine` builds its `AuditLogger` with `buffer_size=COMPLIANCE_BUFFER_MAX`
  (default 100; a value < 1 is refused at Settings load and by `AuditLogger`, since 0
  would drop every entry meant for persistence). Metadata is bound as the JSON text of
  the entry's Pydantic JSON form (datetimes, UUIDs and decimals become strings, NaN
  becomes `null`), and the rows are built inside the protected block, so a batch that
  fails to serialize or persist is re-buffered rather than lost. `_emit`, `emit_async` and the re-buffer after a failed
  `flush_to_store` keep the newest `buffer_size` entries; dropped entries are counted
  (`AuditLogger.dropped_count`) and logged at WARNING once per `buffer_size` drops.
  Every entry is still emitted to the `l9.audit` logger when it is created.
- `flush_to_store` binds `metadata` as `json.dumps(...)` text.
- `handle_match` / `handle_sync` call `ComplianceEngine.flush_audit_for_request()`,
  which logs a failure and returns 0; the entries stay buffered for the boot periodic
  flush loop (`engine/boot.py` `_compliance_flush_loop`), which is unchanged.

Off: unbounded retention, dict-bound metadata, and request-path propagation.

Rollback: set the flag to `false`.

### Prohibited Factor Violation Audit (RU-063 / CEG-COR-050)

**Flag**: `PROHIBITED_FACTOR_VIOLATION_AUDIT=True` (default on)

`ProhibitedFactorsSpec.auditonviolation` was read nowhere. On: when the domain's
`prohibitedfactors.auditonviolation` is true, `ComplianceEngine.validate_gates`,
`validate_scoring` and `check_sync_request` (endpoint and rows) catch the violation,
emit one entry through `AuditLogger.log_violation` (severity `critical`,
`outcome: denied`, regimes as `compliance_tags`, the subject and field name — never a
value) and re-raise the same error. `handle_match` passes the tenant as the actor. Off:
no entry. The `enforcement` mode value is still not interpreted (DEC-23 pending).

Rollback: set the flag to `false`.

### PII Declared Fields Enforced (RU-064 / CEG-COR-051)

**Flag**: `PII_DECLARED_FIELDS_ENFORCED=True` (default on; takes effect while
`PII_REDACTION_RECURSIVE` is also on)

`ComplianceEngine` probed `compliance.pii.additional_fields`, which `PIISpec` does not
have, so declared fields no built-in hint matched survived raw (healthcare-referral
`name`, `patientid`; legal-discovery `clientname`), `handling: hash` became deletion, and
`PIISpec.enabled` was ignored. On, when `compliance.pii` is present and `enabled: true`:
`PIISpec.fields` + `candidatefields` are PII by exact, case-insensitive key at any depth
in addition to built-in inference, and `redact_response` transforms every detected field
per `PIISpec.handling`:

| `handling` | Response value |
|---|---|
| `hash` | `PIIHandler.hash_value` SHA-256 hex digest of the value's string form |
| `redact` | `[REDACTED]` (`engine.compliance.pii.REDACTION_MARKER`) |
| `encrypt`, `tokenize`, other | fail closed to `[REDACTED]`, with a WARNING naming the mode and field paths (decision DEC-22 (a); no key sourcing or token store exists) |

`compliance.pii.enabled: false` (or no `pii` section): built-in inference with key
deletion over dict paths only (never into `candidates`), as `pii_redaction_recursive`
documents for opted-out domains. Ingest-time hashing of stored PII is out of scope. Off: built-in
inference with key deletion for every domain.

Rollback: set the flag to `false`.

### GDPR Erasure correctness (RU-066 / CEG-COR-054) — no new flag

Still gated by `GDPR_ERASURE_ENABLED=False` / `GDPR_DRY_RUN=True` (§2). With erasure
enabled, `erase_subject` now uses the domain's `compliance.gdpr_subject_id_field`
(sanitized; default `data_subject_id`) for both the dry-run count and the deletion, calls
`gdpr_erase_subject($1, $2)` with `performed_by` = tenant on the PacketStore pool
(`PacketStore.get_pool()`, only while `PACKET_STORE_ENABLED`; otherwise the summary
reports `packets_deleted: null`, `packets_erasable: false`), stores an integer count, and
records the `PII_ERASURE` CRITICAL entry in the domain's `ComplianceEngine` audit trail
before any deletion. **Release gate:** do not enable live erasure in any environment
before the admin tenant-binding fix (RU-003, CEG-COR-003) is deployed with it — until
then `domain_id` is caller-supplied.

**Tenant-scoped packet erasure (RU-066 follow-up, PR #352 review).** The packet step
erases only the requesting tenant's packets: `gdpr_erase_subject(subject_id,
performed_by, tenant)` filters both its audit insert and its delete on
`actor_tenant = tenant`, and `PIIHandler.erase_subject` runs it in one transaction
after `set_config('app.current_tenant', tenant, true)` (while
`PACKET_STORE_RLS_TENANT_SESSION_ENABLED`, as `PacketStore` does since RU-079). Before,
the two-argument function filtered on the subject id only, so on an owner/admin DSN a
subject id reused by another tenant deleted that tenant's packets too, and on the
RLS-enforced `l9_product` DSN the unset `app.current_tenant` hid every row and the call
reported 0. `packet_store.sql` drops the two-argument function, so re-apply it
(§7) together with this release; a packet erasure without a tenant raises `ValueError`
in Python and an exception in SQL. No new flag: the SQL and Python contracts change
together (an off path would call the dropped function), and live erasure stays behind
`GDPR_ERASURE_ENABLED=False` / `GDPR_DRY_RUN=True`.

**Durable pre-delete audit and packet dry-run scope (RU-066 follow-up, plan T13).** The
`PII_ERASURE` entry is flushed with `ComplianceEngine.flush_audit_before_erasure()`, which
is strict: `COMPLIANCE_AUDIT_SOFT_SINK` (RU-065, a request continues past a failed flush)
does not apply to it. With no `POSTGRES_DSN` audit pool, or when the flush raises, the
admin call fails with `ExecutionError` and **nothing** is deleted (the entry stays
buffered for the periodic flush). Before, the soft request-path flush logged the failure
and the packet and graph deletes ran with no durable audit entry. Operator consequence:
live erasure now requires `POSTGRES_DSN` (the audit pool) in addition to
`PACKET_STORE_DSN`. The dry run also reports `would_affect.packets`: the requesting
tenant's PacketStore rows for the subject id (`actor_tenant = tenant`, read in one
transaction with the tenant GUC set, so the `l9_product` role counts the rows it would
erase), `null` while `PACKET_STORE_ENABLED` is off (not erasable), `-1` when the count
fails. No new flag: both changes sit on the same off-by-default path
(`GDPR_ERASURE_ENABLED=False`) and restore the "audited before deleted" invariant §2
already states. Verified by `tests/unit/test_wave6_dormant_features.py`
(`test_erase_subject_failed_audit_flush_blocks_every_delete`,
`test_erase_subject_without_audit_sink_blocks_every_delete`) and the two-tenant
PostgreSQL scenarios in `tests/integration/test_packet_store_pg.py`.

### Causal Attribution Unique Touchpoints (RU-067 / CEG-COR-055)

**Flag**: `CAUSAL_ATTRIBUTION_UNIQUE_TOUCHPOINTS=True` (default on)

`AttributionCalculator` received one row per causal PATH (`ORDER BY distance ASC`) and
weighted rows, so a touchpoint reached by several paths split mass or had its credit
overwritten (diamond: linear summed to 0.6, first_touch to 0). On: rows are collapsed to
unique touchpoint ids keeping the first (minimum-distance) occurrence before weighting,
and that row's link ages feed temporal decay; `total_touchpoints` counts unique ids.
Model ordering semantics are unchanged (DEC-27 pending). Off: per-row weighting.

Rollback: set the flag to `false`.

### Causal Edge Declared Identity (RU-068 / CEG-COR-057, PO-04)

**Flag**: `CAUSAL_EDGE_DECLARED_IDENTITY=True` (default on)

`CausalCompiler.compile_causal_edge_create` matched both ends by `entity_id` (an outcome
node carries only `outcome_id`, so the edge never matched), emitted
`AND $confidence >= <literal>` with no `WHERE` when `temporal_validation` was false, and
MERGEd on `{confidence, mechanism, created_at: datetime()}` so every repeat created a
parallel edge. On:

- identity per label from `engine/causal/identity.py`: `outcome_id` for
  `feedbackloop.outcome_node_label`, else the sync endpoint `idproperty`, else the node's
  first required property, else `entity_id` (same rule as `handle_match`, honouring
  `SPEC_DECLARED_CANDIDATE_ID`); `handle_outcomes` passes the candidate's id property
  explicitly;
- one `WHERE` (only when non-empty) joining `source.created_at < target.created_at` and
  `$confidence >= $confidence_threshold` (bound by `handle_outcomes`);
- `MERGE (source)-[r:TYPE]->(target)` with `ON CREATE SET` created_at / confidence /
  mechanism and `ON MATCH SET` confidence / mechanism;
- `AttributionCalculator` (PO-04) returns `CASE WHEN touchpoint:<Label> THEN
  coalesce(touchpoint.<declared id>, touchpoint.entity_id) … ELSE touchpoint.entity_id END`
  over the causal-edge source labels instead of `touchpoint.entity_id`.

Off: the legacy Cypher, kept for rollback although it cannot match outcome nodes.

Rollback: set the flag to `false`.

### Causal Validator Batch Edge Type (RU-069 / CEG-COR-058)

**Flag**: `CAUSAL_VALIDATOR_BATCH_EDGE_TYPE=True` (default on)

With a `CausalSubgraphSpec` (the production type), `CausalEdgeRuntimeValidator` found
the edge spec via `record["edge_type"]`; `handle_sync` records carry no such key, so a
declared `temporal_validation: true` was never enforced on sync batches. On:
`validate_batch` passes its `edge_type` to `_validate_record`, and enforcement is
`CausalSpec.enforce_temporal_precedence` when present, else the matching
`CausalEdgeSpec.temporal_validation`. Tenants syncing inverted timestamps now see
`rejected_count` / `all_rejected`. Off: the record-key lookup.

Rollback: set the flag to `false`.

### safe_eval Rejects Keyword Arguments (RU-076 / CEG-COR-125)

**Flag**: `SAFE_EVAL_REJECT_KEYWORDS=True` (default on)

`engine/utils/safe_eval.py` evaluated only positional arguments of a whitelisted call,
so `round(2.567, ndigits=2)` returned `3`. On: any `ast.Call` with keywords raises
`ValueError("keyword arguments not allowed")`. No shipped expression uses keywords.
The companion resource bounds (RU-070: `_MAX_INT_BITS`, `_MAX_ROUND_NDIGITS`, overflow
as `ValueError`) are guard-only and carry no flag. Off: keywords silently dropped.

Rollback: set the flag to `false`.

### Causal Serializer Variable Depth (RU-074 / CEG-COR-121)

**Flag**: `CAUSAL_SERIALIZER_VARIABLE_DEPTH=True` (default on)

`CausalSubgraphSerializer.serialize_neighborhood` matched `(start)-[r:T]->(neighbor)`, a
single hop, so `match_explanation.depth_reached` never exceeded 1. On: the pattern is
`[:T1|T2*1..N]` (`[*1..N]` without declared causal edges) where N is `max_depth` as an
integer clamped to `[1, causal.chain_depth_limit]` and interpolated as a literal — Cypher
does not accept a parameter as a variable-length bound; `$max_depth` carries N and
`LIMIT $max_nodes` still bounds the result. `handle_match` keeps its depth policy
(`min(chain_depth_limit, 2)`). Off: the one-hop pattern.

Rollback: set the flag to `false`.

### Counterfactual Dedupe and Spec Label (RU-075 / CEG-COR-122)

**Flag**: `COUNTERFACTUAL_DEDUPE_AND_SPEC_LABEL=True` (default on)

`CounterfactualGenerator._diff_fingerprints` appended
`(change_weight, <dim>_weight_increase)` once per qualifying positive outcome, so
duplicates consumed `max_scenarios_per_outcome` slots, and both fingerprint queries
hardcoded `:TransactionOutcome`. On: a seen-set on `(intervention_type, key_difference)`
suppresses duplicates, and `handle_outcomes` passes
`sanitize_label(feedbackloop.outcome_node_label)` (default `TransactionOutcome`) as the
queried label. Off: duplicates and the literal label.

Rollback: set the flag to `false`.
## 24. Correctness remediation lane F (plan 0d84ec8)

Hardening fixes from the CEG correctness remediation plan. Each restores an
invariant the code already claimed, so each ships `True`; set the env var to
`false` to restore the previous behaviour verbatim.

| Flag (env var) | Default | Unit | On | Off |
| `neo4j_circuit_release_cancelled_probes` (`NEO4J_CIRCUIT_RELEASE_CANCELLED_PROBES`) | `true` | RU-094 / CEG-COR-035 | A HALF_OPEN probe that ends in `asyncio.CancelledError` returns its probe slot (only within the HALF_OPEN cycle it was taken in) and records neither success nor failure | The slot leaks; `half_open_max_calls` cancelled probes leave the breaker rejecting every call until process restart |
| `neo4j_circuit_ignore_client_errors` (`NEO4J_CIRCUIT_IGNORE_CLIENT_ERRORS`) | `true` | RU-093 / CEG-COR-034 | `neo4j.exceptions.ClientError` and subclasses are a neutral breaker outcome: re-raised unchanged (absent-database translation still applies), not counted, streak not reset, HALF_OPEN probe slot returned; counted in `BreakerMetrics.total_neutral_errors`. ServiceUnavailable, SessionExpired, TransientError, DatabaseError, OSError and everything else still count. Exception: the credential classes `AuthError` (incl. `TokenExpired`) and `Forbidden` are `ClientError` subclasses but still count as failures, so bad or expired credentials open the breaker (owner decision 2026-10-29) | Every exception counts; five consecutive client errors from one tenant open the breaker for all tenants |
| `neo4j_provision_inside_breaker` (`NEO4J_PROVISION_INSIDE_BREAKER`) | `true` | RU-095 / CEG-COR-170 | The coroutine admitted by the breaker provisions on first use and then runs the query/write, so an OPEN breaker raises `CircuitOpenError` with no `system` call; provisioning semantics (once per database per process, shielded shared task, failed CREATE retried later) unchanged; a malformed database name is still refused before admission as a `ValueError` | Every call for a not-yet-ensured database awaits a `CREATE DATABASE` on `system` (up to the 60 s acquisition timeout) before the breaker can reject it |
| `gds_dag_job_identity_by_name` (`GDS_DAG_JOB_IDENTITY_BY_NAME`) | `true` | RU-096 / CEG-COR-036 | `GDSScheduler._build_execution_dag` keys nodes by `job.name` in declaration order; a `depends_on` entry (an algorithm name, per the schema) depends on every job with that algorithm; unknown dependencies are ignored with a warning and cycle members land in a final fallback wave as before. Every declared cron job is registered with APScheduler, and the registration log reports the number actually registered. Job-name uniqueness is not enforced (DEC-42, pending decision) | The DAG is keyed by algorithm: of two `louvain` jobs only the last declared is scheduled |
| `gds_temporal_recency_decay_existing` (`GDS_TEMPORAL_RECENCY_DECAY_EXISTING`) | `true` | RU-097 / CEG-COR-037 | Three statements per run: create aggregates per (src, dst) — `max(t.<timestamp_property>)` is `last_event_date`, its age drives `recency_score`, and (when `count_property` is declared) `recent_count` sums `coalesce(t.<count_property>, 1)` over the pair's interactions within `half_life_days`; refresh recomputes `recency_score` of every existing overlay edge from its `last_event_date`; prune deletes edges with `recency_score < prune_below` or age `> max_age_days`. The first run prunes stale overlay edges accumulated so far (derived data) | The overlay edge carries whichever matching interaction row was SET last, and an edge whose interactions aged out is never re-scored, so the defaults never prune it |
| `gds_cooccurrence_deterministic_prune` (`GDS_COOCCURRENCE_DETERMINISTIC_PRUNE`) | `true` | RU-098 / CEG-COR-168 | Per-node keep sets are computed from edges ordered by `r.weight` DESC, `elementId(r)`; a node with degree > 10 keeps its `floor(degree^0.86)` strongest edges, a node with degree <= 10 keeps all of them, and an edge is deleted only when it is outside the keep set of both endpoints. Identical input gives an identical surviving edge set. Alpha 0.86, the > 10 floor and the Step-1 MERGE are unchanged | Unordered `collect` on pre-deletion degree with deletion from either endpoint: K15 hubs end with 1-5 edges instead of 9, leaves of a hub lose their only edge, and the survivors differ run to run |
| `gds_history_utc_timestamps` (`GDS_HISTORY_UTC_TIMESTAMPS`) | `true` | RU-100 / CEG-COR-086 | `GDSScheduler.execute_job` takes start, duration and the history `timestamp` from `datetime.now(tz=UTC)`; the ISO-8601 timestamp gains a `+00:00` offset and still parses with `datetime.fromisoformat`. Consumers (`handlers.py` gds_health, `boot.py`) are unchanged and keep their naive-as-UTC fallback | Naive local time read as UTC by consumers: a just-run job looks 7 h old under America/Los_Angeles and -9 h under Asia/Tokyo |
| `entity_resolution_merge_integrity` (`ENTITY_RESOLUTION_MERGE_INTEGRITY`) | `true` | RU-101 / CEG-COR-059, -060, -061 | `resolve_batch` rejects a `threshold` that is not a number in [0, 1] (`ValueError`) and threads it to `resolve_entity` (new optional `threshold` kwarg); already-resolved entities are neither candidates nor canonical; the canonical is the highest-degree entity with `RESOLVED_FROM` excluded from degree and ties broken by `entity_id` ascending; a member is merged only when its similarity to the canonical meets the threshold and the edge stores that measured similarity; the edge is `MERGE`d with `ON CREATE SET`; batch runs skip merged entity ids and the canonical and count only new edges. Responses gain `merged_entity_ids`. Only reachable when a domain enables `semantic_registry` (none shipped) | Batch over three identical entities writes six edges (`total_merged` 6); a degree tie can produce an e1<->e2 cycle; the batch `threshold` is ignored; a member is merged into a canonical it is 0.33 similar to, with confidence defaulting to 1.0 |
| `resolution_behavioral_label_scoped` (`RESOLUTION_BEHAVIORAL_LABEL_SCOPED`) | `true` | RU-103 / CEG-COR-177 (+ PO-04 extension of RU-002) | `SimilarityScorer._behavioral_similarity(a, b, label)` matches `(a:<label> {<id>: $a_id})` / `(b:<label> {<id>: $b_id})`, label and id property through `sanitize_label`, ids as `$params`. The id property is the label's declared id (sync `idproperty`, else first required property, else `entity_id`; `entity_id` while `spec_declared_candidate_id` is off), read from the `DomainSpec` passed to `EntityResolver` / `SimilarityScorer` (new optional `domain_spec` kwarg) or, when none is passed, from the engine's domain loader for the domain id. Only reachable when a domain enables `semantic_registry` | `(a {entity_id: $a_id})` matches nodes of any label, so `RESULTED_IN` outcomes of a same-id node of another label are counted |

### Rollback

Set the flag's env var to `false` and restart the process. A process already
wedged by the pre-fix behaviour needs a restart either way.

## 25. Correctness remediation lane I (plan 0d84ec8, round 2) — domain spec validation

Every flag below tightens `engine/config/schema.py` validation, so a spec that
violates the rule stops loading (`DomainSpec.model_validate` raises). All default
`true`; every shipped pack and `domains/_template.yaml` validate with all of them on.

| Flag (env var) | Default | Unit | On | Off |
|---|---|---|---|---|
| `weight_ceiling_includes_unscoped` (`WEIGHT_CEILING_INCLUDES_UNSCOPED`) | `true` | RU-044 / CEG-COR-024 | Under `domain_strict_validation`, for each `queryschema.matchdirections` entry (and any direction a dimension is scoped to) the default weights of unscoped dimensions plus the dimensions scoped to it must be <= 1.0; the error names the direction | Each scope bucket (`*` and every direction) is summed alone, so `{'*': 0.6, 'd1': 0.6}` loads with an effective `d1` sum of 1.2 |
| `traversal_open_range_rejected` (`TRAVERSAL_OPEN_RANGE_REJECTED`) | `true` | RU-045 / CEG-COR-038 | `TraversalSpec.validate_traversal_hops` matches `\*(\d*)(\.\.)?(\d*)`: a min < 1 is refused (`hop count must be >= 1`), a `..` with no upper bound is refused (`has no explicit upper bound`), an explicit upper bound (or the fixed count) above `max_hop_hard_cap` is refused, bare `*` stays refused (`exceeds hard cap`). `_validate_traversal_pattern` additionally requires `*N` or `*[M]..N` in the relationship, so `*`, `*N..`, `*..` fail `pattern does not match allowed structure`. No shipped pack declares a variable-length step | `*1..` / `*2..` load (the regex matched `*1` and read one hop) and the assembler emits them unbounded |
| `gate_name_uniqueness` (`GATE_NAME_UNIQUENESS`) | `true` | RU-049 / CEG-COR-076 | `DomainSpec.validate_cross_references` raises `Duplicate gate name(s): [<names>]` (sorted) when any gate name repeats; independent of `domain_strict_validation`. No shipped pack repeats a gate name | Duplicates load; the second gate's compiled literal overwrites the first |
| `domain_undeclared_param_rejected` (`DOMAIN_UNDECLARED_PARAM_REJECTED`) | `true` | RU-050 / CEG-COR-077 | Under `domain_strict_validation`, when the spec declares any queryschema field or derived parameter, a gate whose string `queryparam` is undeclared raises `ValueError("Gate '<g>' references undeclared parameter '<q>'")`. Declared means: the name itself, its base after stripping `_min`/`_max`/`_start`/`_end`, or, for a `temporalrange` gate, both `<q>_start` and `<q>_end` (the names the compiler derives). Literal (scalar) operands are exempt | The same message is a WARNING log and the spec loads; admission (`admission_defects`) is the only refusal |
| `gate_spec_forbid_unknown_keys` (`GATE_SPEC_FORBID_UNKNOWN_KEYS`) | `true` | RU-053 / CEG-COR-081 | A `GateSpec` `model_validator(mode="before")` raises `Gate '<name>' has unknown key(s) [<keys>]` for any input key outside `GateSpec.model_fields`, adding `'<key>' (did you mean '<field>'?)` when the key matches a field with underscores removed (e.g. `null_behavior` → `nullbehavior`). No aliases are introduced. Fields added to `GateSpec` later are accepted automatically | Unknown keys are ignored: `null_behavior: fail` leaves `nullbehavior` at its `pass` default and the gate admits NULL operands |
| `domain_version_semver_enforced` (`DOMAIN_VERSION_SEMVER_ENFORCED`) | `true` | RU-057 / CEG-COR-152 | `DomainMetadata.version` must fully match `(0\|[1-9]\d*)\.(0\|[1-9]\d*)\.(0\|[1-9]\d*)` optionally followed by `-seed`, `-discovered`, `-inferred`, `-proposed`, `-reviewed` or `-production` (DOMAIN_SPEC_VERSIONING.md); anything else raises `domain.version '<v>' is not semver`. `TemplateProvenance.version` is unchanged. Shipped pack versions (0.1.0, 1.0.0, 1.0.1, 1.1.0, 1.2.0) validate | Any string, including `latest`, `v2` and dates, is accepted |
## 26. Correctness remediation lane J (plan 0d84ec8, round 2)

|---|---|---|---|---|
| `geodecay_declared_longitude` (`GEODECAY_DECLARED_LONGITUDE`) | `true` | RU-037 / CEG-COR-011 (DEC-14 option A) | A `geodecay` dimension's declared `candidateprop_lon` / `queryprop_lon` name the longitude operands (`point({latitude: candidate.<candidateprop>, longitude: candidate.<candidateprop_lon>})` vs `point({latitude: $<queryprop>, longitude: $<queryprop_lon>})`); undeclared they default to `candidate.lon` / `$lon`, so dimensions without them (PlasticOS `geo_proximity`, `active_supply_geo`) compile byte-identically. `freight-matching` `deadhead_distance` measures Carrier `currentlat`/`currentlon` to Load `pickuplat`/`pickuplon` | The declarations are ignored and `candidate.lon` / `$lon` are emitted; `freight-matching` is withheld again by the scoring admission check for its unbound `$lon` |

### Rollback

Set `GEODECAY_DECLARED_LONGITUDE=false` and restart. Domain-spec changes of this
lane (plasticos 1.3.0, mortgage-brokerage 1.1.0, freight-matching 1.2.0) roll
back by restoring the previous spec file; the loader hot-reloads on mtime.
## 27. Correctness remediation lane K (plan 0d84ec8)

Round-2 fixes from the CEG correctness remediation plan. Each ships `True`; set
the env var to `false` to restore the previous behaviour verbatim.

|---|---|---|---|---|
| `gds_shared_scheduler_registry` (`GDS_SHARED_SCHEDULER_REGISTRY`) | `true` | RU-007 / CEG-COR-016 | `GraphLifecycle.startup` registers each started scheduler in `get_state().gds_schedulers[domain_id]` (one per domain id); the W6-03 GDS health probe reads that registry at probe time; `_get_or_create_scheduler` (admin `trigger_gds`, `gds_status`, `gds_trigger`, `gds_health`) reuses the registered instance, and for a domain boot did not start it registers the jobs and starts APScheduler only when `gds_enabled` is true, so `GDS_ENABLED=false` runs no cron job anywhere while manual triggers still execute; `EngineState.shutdown` (called by `GraphLifecycle.shutdown`) stops every started scheduler and skips one that never started | Boot keeps its schedulers in `GraphLifecycle._schedulers`, which nothing stops; the first admin `gds_*` call starts a second scheduler for the same domain (cron jobs run twice, `gds_status` reports the duplicate's empty history) and starts it even with `GDS_ENABLED=false` |
| `outcome_schema_unified` (`OUTCOME_SCHEMA_UNIFIED`) | `true` | RU-006 / CEG-COR-015 (R1a-DEC-02 first alternative) | `_build_outcome_write` is the one outcome writer: `handle_outcomes` and sync `entity_type=outcome` both create the domain's `feedbackloop.outcome_node_label` node with the canonical `success`/`failure`/`partial` (sync maps `positive`→`success`, `negative`→`failure`, `neutral`→`partial`; canonical values pass through; anything else is skipped), link it by RESULTED_IN from the candidate's declared id, and apply the R3 causal check per item (sync reports `rejected_count` when edges were suppressed). The node carries `dimension_scores` (sorted-key JSON of the optional `fingerprint.dimension_scores` map; null when absent) and `was_selected` (optional bool); a malformed value is a `ValidationError` on `outcomes` and a skipped item on sync. `score_feedback` RETURNs `o.dimension_scores`, decoded with `decode_outcome_map`, and `OutcomeFeedback` treats `success` as positive and `failure` as negative (`partial` is neither). `discover_weights` already reads `dimension_scores` / `was_selected`. Existing `:Outcome` nodes are not migrated | Sync writes `:Outcome` with `positive`/`negative`/`neutral` and HAS_OUTCOME; `handle_outcomes` writes no `dimension_scores` / `was_selected`; `score_feedback` and `discover_weights` therefore never produce output |
| `strict_null_gates` (`STRICT_NULL_GATES`, existing W1-03 flag; meaning fixed by R1a-DEC-03) | `true` | RU-010 / CEG-COR-023 | `handle_match` calls `GateCompiler.validate_gates(resolved_query, match_direction=…, reject_unbound=True)` after defaults backfill and derived resolution, over the gates active for the direction (`matchdirections` empty or containing it). A gate whose named `queryparam` is missing/null and is undeclared (neither a queryschema field nor a derived parameter) or a `required` queryschema field raises `ValidationError` naming every such gate; a null optional or derived parameter is left to the gate's `nullbehavior` (C-14) and logged as a WARNING once per gate. Literal operands and gates without a named parameter are not checked. Required queryschema fields are therefore enforced for gates that read them (e.g. plasticos `polymer_type`) | `validate_gates` is never called on the match path; a null undeclared parameter reaches Neo4j as a missing parameter |
| `match_request_contract_admission` (`MATCH_REQUEST_CONTRACT_ADMISSION`) | `true` | RU-012 / CEG-COR-063 (R1a-DEC-04 "admit both") | `handle_match` validates a payload that carries `contract_version` with `admit_payload(MatchRequest, payload, DomainContract.from_spec(<tenant's pack>))` — tenant-bound `domain`, declared `direction`, `extra=forbid`, transport fields refused, failures raised as `ValidationError` before any query — and continues with `{query: query.facts, match_direction: direction, top_n}`. Payloads without `contract_version` (legacy `MatchPayload`) are unchanged. The response stays the legacy shape | The `MatchRequest` example is rejected with `Missing required field 'match_direction'` |
| `gate_role_exemption` (`GATE_ROLE_EXEMPTION`) | `true` | RU-015 / CEG-COR-080 (R1a-DEC-05 "withhold") | `engine.config.loader.admission_defects` adds `<gate>: roleexempt … is not consumed — match takes no request role` for a gate whose `roleexempt` is set and `<gate>: relaxedpenalty … is not consumed — relaxed matching is not exposed` for a gate that declares `relaxedpenalty` (explicitly, any value), so `withholding_reason` withholds the pack unless `UNVALIDATED_DOMAIN_PACKS_ENABLED`. No shipped pack declares either field. `schema.py`, the compiler's `role` / `compile_relaxed` support and the match payload are unchanged | The fields are accepted and silently ignored by every match |

## 32. Stage 2 follow-up — declared domain vocabulary (owner decisions 2026-11-11)

The engine borrows no pack's vocabulary as a default. Each behaviour below ships
`True` because it restores "the domain spec is the single source of truth" and
changes nothing for a pack that declares what it uses; every served pack
(freight-matching, mortgage-brokerage, plasticos) declares it. Set the env var to
`false` to restore the previous behaviour verbatim (C-21 rollback).

| Flag (env var) | Default | Behaviour on | Off (rollback) |
|---|---|---|---|
| `outcome_vocabulary_required_enabled` (`OUTCOME_VOCABULARY_REQUIRED_ENABLED`) | `true` | `FeedbackLoopSpec.outcome_node_label` defaults to `None`. `handle_outcomes`, sync `entity_type=outcome`, admin `score_feedback` / `generate_calibration_pairs` / `discover_weights`, the feedback loop (signal weights, propagation, pattern matching, drift), causal attribution and a `preference_attention` dimension without `metadata.outcome_node` read the domain's declared label; on a pack that declares none they raise before any Cypher — `OutcomeVocabularyUndeclaredError` (a `ValidationError`) from handlers, `OutcomeNodeLabelUndeclaredError` (a `ValueError`) from the engine modules — naming the domain and the operation. freight-matching and mortgage-brokerage declare `outcome_node_label: TransactionOutcome` (the label their graphs already hold), plasticos already declared it, so no graph migrates | An undeclared label resolves to the former default `TransactionOutcome` |

The outcome *edge* (`feedbackloop.outcome_edge_type`, default `RESULTED_IN`) is
not part of this section: its split stays deferred with A09.

### Validation

`tests/unit/test_outcome_vocabulary_required.py` (synthetic packs: orbital-logistics
declares `DockingResult`, cheese-aging declares nothing).

### Rollback

Set `OUTCOME_VOCABULARY_REQUIRED_ENABLED=false` and restart.
