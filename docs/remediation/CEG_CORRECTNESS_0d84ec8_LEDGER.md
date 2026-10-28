<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [remediation, audit, ledger]
owner: engine-team
status: active
/L9_META -->

# CEG correctness remediation — campaign ledger (plan 0d84ec8)

Plan `plan.ceg.correctness-remediation.0d84ec8531ba.v1`, audited revision `0d84ec8`. Generated from `remediation_plan.yaml` (finding dispositions, units, decisions) and this branch's commit history: a unit is DONE when a commit names it; every other outcome is recorded below with its evidence. Owner authority: Ask-First approval for RU-007/044/045/049/050/053/057/080 and adoption of each decision's recommended alternative (owner directive, 2026-10-27).

See [VERIFICATION.md](VERIFICATION.md) for the test, integration, flag-OFF and cross-PR evidence.

## Units

| Status | Count |
|---|---|
| DONE | 150 |
| DONE_THEN_RETIRED | 1 |
| RETIRED | 11 |

| Unit | Cluster | Sev | Plan state | Findings | Status | Flag | Evidence |
|---|---|---|---|---|---|---|---|
| RU-001 | R1a | HIGH | READY | 001 | DONE | `match_dimension_projection` | a9f94f9811 fix(match): project per-dimension scores out of the match query (RU-001; CEG-COR-001) |
| RU-002 | R1a | HIGH | READY | 002 | DONE | `spec_declared_candidate_id` | d79386053d fix(handlers): use spec-declared node identity at every non-match id lookup (RU-002; CEG-COR-002); 5d3fa7617d test(outcomes): seed the declared candidate id in the causal-integrity fixture (RU-002; CEG-COR-002) |
| RU-003 | R1a | HIGH | READY | 003 | DONE | `admin_tenant_bound_domain` | 45d2ee3003 fix(admin): bind every admin subaction's domain to the packet tenant (RU-003; CEG-COR-003) |
| RU-004 | R1a | MEDIUM | READY | 014 | DONE | `resolve_requires_created_edge` | 87719dc4a0 fix(resolve): legacy resolve fails when no RESOLVED_FROM edge is created (RU-004; CEG-COR-014) |
| RU-005 | R1a | LOW | READY | 056 | DONE | `outcome_map_json_encoding` | 3d0d862c03 fix(outcomes): decodable encoding for outcome map fields (RU-005; CEG-COR-056) |
| RU-006 | R1a | MEDIUM | GATED (DEC-02) | 015 | DONE | `outcome_schema_unified` | df91af8206 fix(outcomes): one outcome record schema for all writers and readers (RU-006; CEG-COR-015) |
| RU-007 | R1a | MEDIUM | READY | 016 | DONE | `gds_shared_scheduler_registry` | f26d4d8a27 fix(gds): single GDS scheduler registry in EngineState (RU-007; CEG-COR-016) |
| RU-008 | R1a | LOW | READY | 017 | DONE | `capability_auth_enabled` | 928573455b fix(admin): enforce admin:write capability in handle_admin (RU-008; CEG-COR-017) |
| RU-009 | R1a | MEDIUM | READY | 018 | DONE | `match_effective_weight_validation` | 5f65d40dc1 fix(match): validate the effective (merged) request weights (RU-009; CEG-COR-018); db6278c88e fix(match): report a non-numeric request weight as ValidationError (RU-009 follow-up) |
| RU-010 | R1a | MEDIUM | GATED (DEC-03) | 023 | DONE | `strict_null_gates` | 75ad952eda fix(gates): strict_null_gates means the R1a-DEC-03 contract (RU-010; CEG-COR-023); d1c3ee3a22 test: reconcile round-2 lane fixtures (RU-010 x RU-046, RU-010 x RU-050); 240c7cd5d6 fix(contracts): MatchRequest example uses the plasticos query vocabulary (RU-012 x RU-010) |
| RU-011 | R1a | MEDIUM | READY | 033 | DONE | `gate_where_row_scope` | f4152dd2f0 fix(match): scope gate predicates to candidate rows (RU-011; CEG-COR-033); 23cce72982 test(integration): seed the polymer edge in the RU-011 row-scope fixture |
| RU-012 | R1a | MEDIUM | GATED (DEC-04) | 063 | DONE | `match_request_contract_admission` | 8254c74a2b feat(match): admit contract-versioned MatchRequest alongside MatchPayload (RU-012; CEG-COR-063); 240c7cd5d6 fix(contracts): MatchRequest example uses the plasticos query vocabulary (RU-012 x RU-010) |
| RU-013 | R1a | LOW | READY | 074 | DONE | `health_probe_domain_count_only` | bb8f6f1db6 fix(health): health probe stops listing domain ids (RU-013; CEG-COR-074) |
| RU-014 | R1a | LOW | READY | 075 | DONE | — | 938098b228 fix(handlers): add contract-required status/subaction to success returns (RU-014; CEG-COR-075) |
| RU-015 | R1a | LOW | GATED (DEC-05) | 080 | DONE | `gate_role_exemption` | 38d0bcd628 fix(loader): withhold packs declaring unserved roleexempt/relaxedpenalty (RU-015; CEG-COR-080) |
| RU-016 | R1a | LOW | READY | 171 | DONE | `query_defaults_before_derived` | 2021d988ba fix(match): backfill query-schema defaults before resolving derived parameters (RU-016; CEG-COR-171) |
| RU-017 | R1b | HIGH | READY | 006 | DONE | `pricealignment_float_division` | bf71fc9d3a fix(scoring): pricealignment float division of operands (RU-017; CEG-COR-006) |
| RU-018 | R1b | MEDIUM | READY | 025 | DONE | `arbitration_zero_score_present` | 83e7809c14 fix(scoring): arbitration treats numeric 0.0 as a score, not absence (RU-018; CEG-COR-025) |
| RU-019 | R1b | LOW | READY | 026 | DONE | `arbitration_objective_direction` | 00e725674d fix(scoring): arbitration Pareto honours objective direction and front_size_limit (RU-019; CEG-COR-026) |
| RU-020 | R1b | LOW | READY | 027, 028, 029 | DONE | `scoring_admission_check_enabled` | 8d2742f5b7 fix(config): admission withholds packs whose scoring dimensions cannot execute (RU-020; CEG-COR-027, CEG-COR-028, CEG-COR-029) |
| RU-021 | R1b | LOW | GATED (DEC-08) | 082 | DONE | — | 503c1cc04c fix(scoring): preference_attention samples the K most recent successes once (RU-021; CEG-COR-082) |
| RU-022 | R1b | MEDIUM | READY | 030 | DONE | `learned_weights_tenant_keyed` | 8d643b1b0c fix(scoring): load_learned_weights reads the writer's DimensionWeight keys (RU-022; CEG-COR-030) |
| RU-023 | R1b | LOW | READY | 083 | DONE | `learned_weight_renormalization` | b2fe85bb18 fix(scoring): learned weights keep the C-22 ceiling and accept 0.0 (RU-023; CEG-COR-083) |
| RU-024 | R1b | LOW | READY | 031 | DONE | — | 56a71595cd fix(scoring): weight discovery computes real NDCG against selection labels (RU-024; CEG-COR-031) |
| RU-025 | R1b | LOW | GATED | 032 | DONE | — | 2e116eccb8 fix(feedback): OutcomeFeedback treats a missing class as no evidence (RU-025; CEG-COR-032) |
| RU-026 | R1b | LOW | READY | 084 | DONE | — | 9d845f8da6 fix(scoring): calibration pairs skip out-of-range scores per record (RU-026; CEG-COR-084) |
| RU-027 | R1b | LOW | READY | 116 | DONE | `signal_weight_proportion_ci` | 7df6e8fd03 fix(feedback): signal-weight confidence interval from the win-rate proportion (RU-027; CEG-COR-116) |
| RU-028 | R1b | LOW | READY | 117 | DONE | `propagation_single_application` | 4995af9085 fix(feedback): ScorePropagator applies the factor once per event, bounded (RU-028; CEG-COR-117) |
| RU-029 | R1b | LOW | READY | 118 | DONE | `drift_requires_both_windows` | 896ee56f9c fix(feedback): drift detector reports insufficient data when either window is empty (RU-029; CEG-COR-118) |
| RU-030 | R1b | LOW | READY | 123 | DONE | — | c41d11e8a8 fix(feedback): pattern matcher bounded frequency correction and persisted config source (RU-030; CEG-COR-123) |
| RU-031 | R1b | LOW | READY | 166 | DONE | — | f3ca9937ba fix(scoring): W1-02 clamp maps NaN dimension scores to 0.0 (RU-031; CEG-COR-166) |
| RU-032 | R1b | LOW | READY | 167 | DONE | `monoculture_weighted_contribution` | f469c2093c fix(match): feed weighted contributions to the monoculture check (RU-032; CEG-COR-167) |
| RU-033 | R2 | HIGH | GATED (DEC-12) | 004 | DONE | — | 6514e657e2 fix(domains): PlasticOS active-supply gate as an implication with a true default (RU-033; CEG-COR-004) |
| RU-034 | R2 | HIGH | READY | 005 | DONE | — | 70d82b1292 fix(domains): PlasticOS food-grade requirement as an implication using the existing threshold gate (RU-034; CEG-COR-005) |
| RU-035 | R2 | UNKNOWN | GATED |  | DONE | — | d40b6a1466 fix(domains): mortgage VA-eligibility requirement as an implication (RU-035; CEG-COR-005) |
| RU-036 | R2 | HIGH | READY | 007 | DONE | `match_collapse_traversal_rows` | ccdfcfc18c fix(match): return each candidate at most once regardless of traversal paths (RU-036; CEG-COR-007) |
| RU-037 | R2 | HIGH | GATED (DEC-14) | 011 | DONE | — | ccdb026336 fix(scoring): freight-matching scoring binds Carrier/Load coordinates and rates (RU-037; CEG-COR-011) |
| RU-038 | R2 | HIGH | GATED (DEC-15) | 012 | DONE | — | 3e2ae4761d fix(domains): mortgage lender traversal executable; pack dormant pending licensing (RU-038; CEG-COR-012); dc8c0f0595 docs(domains): note the mortgage-brokerage dormancy in the pack index and testing guide (RU-038) |
| RU-039 | R2 | UNKNOWN | READY |  | DONE | `admission_checks_full_match` | e7b3b8cd45 fix(config): admission withholds packs whose traversal or scoring cannot be assembled or whose match Cypher references unbindable parameters (RU-039; contributes CEG-COR-011, CEG-COR-012) |
| RU-040 | R2 | HIGH | READY | 013 | DONE | — | f834ea8c9a fix(domains): PlasticOS constrains facilities to the requested polymer with traversal gates instead of row-multiplying steps (RU-040; CEG-COR-013) |
| RU-041 | R2 | MEDIUM | READY | 019 | DONE | `exclusion_endpoint_validation` | 2488ec2e36 fix(gates): refuse exclusion gates the compiler cannot honour instead of compiling them to a no-op (RU-041; CEG-COR-019) |
| RU-042 | R2 | MEDIUM | READY | 020, 021 | DONE | `gate_null_operands_from_predicate` | a267e8fd5e fix(gates): apply each gate's declared null policy to the operands its predicate actually tests (RU-042; CEG-COR-020, CEG-COR-021) |
| RU-043 | R2 | LOW | READY | 022 | DONE | `composite_subgate_scoping` | 0e14934837 fix(gates): composite gates compose — subgates contribute only through their composite and direction scoping is enforced (RU-043; CEG-COR-022) |
| RU-044 | R2 | MEDIUM | READY | 024 | DONE | `weight_ceiling_includes_unscoped` | 5846cde2c1 fix(config): weight ceiling validated per declared direction including unscoped dimensions (RU-044; CEG-COR-024) |
| RU-045 | R2 | MEDIUM | READY | 038 | DONE | `traversal_open_range_rejected` | 83ba83909a fix(traversal): reject variable-length ranges without an explicit upper bound (RU-045; CEG-COR-038) |
| RU-046 | R2 | MEDIUM | GATED | 069 | DONE | — | 0c0d1b7ebf fix(domains): freight availability admits carriers available on or before pickup (RU-046; CEG-COR-069); d1c3ee3a22 test: reconcile round-2 lane fixtures (RU-010 x RU-046, RU-010 x RU-050) |
| RU-047 | R2 | MEDIUM | GATED (DEC-18) | 070 | DONE | — | 4179799453 fix(domains): mortgage approval_rate reads the lender alias; community dims get a real source (RU-047; CEG-COR-070) |
| RU-048 | R2 | MEDIUM | READY | 071 | DONE | — | 094a4e719d fix(domains): declare PlasticOS polymer edge ingestion with existing taxonomyedges/childsync (RU-048; CEG-COR-071) |
| RU-049 | R2 | LOW | READY | 076 | DONE | `gate_name_uniqueness` | b663ce29b7 fix(config): reject duplicate gate names (RU-049; CEG-COR-076) |
| RU-050 | R2 | LOW | READY | 077 | DONE | `domain_undeclared_param_rejected` | 139e98eedf fix(config): undeclared gate parameter references fail validation (RU-050; CEG-COR-077); d1c3ee3a22 test: reconcile round-2 lane fixtures (RU-010 x RU-046, RU-010 x RU-050) |
| RU-051 | R2 | LOW | READY | 078 | DONE | — | 3436db0400 fix(config): loader cache bounds come from Settings (contract variable names) (RU-051; CEG-COR-078) |
| RU-052 | R2 | LOW | READY | 079 | DONE | `freshness_maxagedays_validated` | 6e53b3ddaa fix(gates): freshness maxagedays defaults only when absent and refuses values < 1 (RU-052; CEG-COR-079) |
| RU-053 | R2 | LOW | READY | 081 | DONE | `gate_spec_forbid_unknown_keys` | eb5a38f6d1 fix(config): reject unknown gate keys and align the contracts on nullbehavior (RU-053; CEG-COR-081) |
| RU-054 | R2 | LOW | GATED (DEC-19) | 150 | DONE | — | 08fa6f08c9 chore(domains): retire the consolidated master spec and its extractor (RU-054; CEG-COR-150); 137fa8448a fix(tools): drop the registry entry for the retired domain_extractor (RU-054 x RU-131); 0c184917c2 test(tools): retired domain_extractor stays unregistered (RU-054 x RU-131) |
| RU-055 | R2 | LOW | READY | 151 | DONE | — | c991ffdc11 fix(domains): normalize served scoring inputs and use the field each computation reads (RU-055; CEG-COR-151) |
| RU-056 | R2 | LOW | READY | 152 | DONE | — | a24b4830cd fix(domains): template validates and demonstrates only executable constructs (RU-056; CEG-COR-152) |
| RU-057 | R2 | UNKNOWN | READY |  | DONE | `domain_version_semver_enforced` | 437cf15e83 fix(config): validate domain.version format (RU-057; CEG-COR-152) |
| RU-058 | R2 | LOW | GATED (DEC-20) | 153 | DONE | — | dac8c4989d fix(domains): dormant packs express capacity and response-time constraints with comparable operands (RU-058; CEG-COR-153) |
| RU-059 | R2 | MEDIUM | GATED | 164 | DONE | — | c395990954 fix(domains): scalar enum matching as equality with the threshold gate (RU-059; CEG-COR-164) |
| RU-060 | R2 | LOW | READY | 169 | DONE | `sync_count_from_write_result` | dc489c1926 fix(sync): UNWINDMATCHSET sync reports the rows actually matched (RU-060; CEG-COR-169); 2ee9120d94 test(sync): RU-060 fixture endpoint names its absent targetedge |
| RU-061 | R3 | HIGH | READY | 008 | DONE | `prohibited_factor_full_coverage` | cbe7931fad fix(compliance): prohibited-factor check covers every compiled property reference, casefolded (RU-061; CEG-COR-008); 303e4df0bc fix(compliance): derive casefolded blocked set from blocked_fields per call (RU-061; CEG-COR-008) |
| RU-062 | R3 | HIGH | READY | 009 | DONE | `pii_redaction_recursive` | cf9f623851 fix(compliance): PIIHandler traverses lists and never mutates its input (RU-062; CEG-COR-009) |
| RU-063 | R3 | MEDIUM | READY | 050 | DONE | `prohibited_factor_violation_audit` | f1df39a04d fix(compliance): audit every prohibited-factor violation before it propagates (RU-063; CEG-COR-050) |
| RU-064 | R3 | MEDIUM | READY | 051 | DONE | `pii_declared_fields_enforced` | 7f212a60f1 fix(compliance): ComplianceEngine applies the domain's declared compliance.pii (RU-064; CEG-COR-051) |
| RU-065 | R3 | HIGH | READY | 052, 053 | DONE | `compliance_audit_soft_sink` | 40da9836af fix(compliance): audit sink is a bounded, soft dependency (RU-065; CEG-COR-052, CEG-COR-053) |
| RU-066 | R3 | MEDIUM | READY | 054 | DONE | — | 18962f98ec fix(compliance): GDPR erasure uses the domain subject field, the PacketStore pool and a pre-deletion audit (RU-066; CEG-COR-054); 871c3feba2 fix(gdpr): scope packet erasure to the requesting tenant (RU-066 follow-up) |
| RU-067 | R3 | MEDIUM | READY | 055 | DONE | `causal_attribution_unique_touchpoints` | bb3da29a0e fix(causal): attribution weights computed over unique touchpoints (RU-067; CEG-COR-055) |
| RU-068 | R3 | MEDIUM | READY | 057 | DONE | `causal_edge_declared_identity` | bb622193c1 fix(causal): CausalCompiler matches declared identities, anchors predicates in WHERE, MERGEs on a stable key (RU-068; CEG-COR-057); 6c6cd60333 fix(causal): attribution trace reads the touchpoint's declared id, not entity_id (RU-068 PO-04; CEG-COR-057) |
| RU-069 | R3 | MEDIUM | READY | 058 | DONE | `causal_validator_batch_edge_type` | 3f50774608 fix(causal): runtime validator resolves temporal validation from the batch edge_type (RU-069; CEG-COR-058) |
| RU-070 | R3 | MEDIUM | READY | 062 | DONE | — | 096576a002 fix(utils): safe_eval bounds operand/result magnitude and round() ndigits (RU-070; CEG-COR-062) |
| RU-071 | R3 | LOW | READY | 089, 090 | DONE | — | ca7c7629d7 fix(chassis): chassis PIIHandler is non-mutating, list-aware, with unambiguous paths (RU-071; CEG-COR-089, CEG-COR-090) |
| RU-072 | R3 | LOW | READY | 119 | DONE | — | 3f1b22ae76 fix(security): sanitize_llm_input normalizes before pattern matching (RU-072; CEG-COR-119) |
| RU-073 | R3 | LOW | READY | 120 | DONE | — | 8ee57b8564 fix(security): strict LLM output forbids extra fields; CypherQueryOutput rejects write clauses by whole keyword (RU-073; CEG-COR-120) |
| RU-074 | R3 | LOW | READY | 121 | DONE | `causal_serializer_variable_depth` | 8fd1e3c01d fix(causal): CausalSubgraphSerializer uses a bounded variable-length pattern up to max_depth (RU-074; CEG-COR-121) |
| RU-075 | R3 | LOW | READY | 122 | DONE | `counterfactual_dedupe_and_spec_label` | 8a5aa3e7ed fix(causal): counterfactual emits one scenario per (intervention, key_difference) and uses the domain outcome label (RU-075; CEG-COR-122) |
| RU-076 | R3 | LOW | READY | 125 | DONE | `safe_eval_reject_keywords` | a1830725ed fix(utils): safe_eval rejects keyword arguments (RU-076; CEG-COR-125) |
| RU-077 | R3 | LOW | READY | 126 | DONE | — | da9a8aa5b5 fix(utils): sanitize_label requires a full match of the C-9 grammar (RU-077; CEG-COR-126) |
| RU-078 | R4 | MEDIUM | READY | 040, 095 | DONE | `packet_store_packet_identity_dedup_enabled` | f49254cc72 fix(packet): PacketStore dedup keyed on packet identity, children written only with their parent (RU-078; CEG-COR-040) |
| RU-079 | R4 | MEDIUM | READY | 042 | DONE | `packet_store_rls_tenant_session_enabled` | 79081464d9 fix(packet): PacketStore sets transaction-local app.current_tenant before every write (RU-079; CEG-COR-042) |
| RU-080 | R4 | LOW | READY | 096 | DONE | — | b3bbcea6d8 fix(packet): packet_store.sql becomes idempotent and non-destructive (RU-080; CEG-COR-096) |
| RU-081 | R4 | MEDIUM | GATED (DEC-31) | 041 | DONE | — | 8fbd31abe3 fix(packet): ship the outcome_jsonb schema that record_outcome requires (RU-081; CEG-COR-041); 373c126532 test(packet): clear contract-scanner SEC-005 hit in the RU-081 DDL test |
| RU-082 | R4 | MEDIUM | GATED (DEC-29,DEC-30) | 039 | DONE | `legacy_chassis_auth_enabled` | d9be12df7a fix(chassis): legacy chassis env guard in the factory, auth registration, W3-01 tenant check (RU-082; CEG-COR-039); 786d3a2772 fix(tools): register the moved chassis auth test at its new path (RU-082 x RU-131) |
| RU-083 | R4 | LOW | GATED (DEC-29) | 087, 088 | DONE | `legacy_chassis_status_mapping_enabled` | 0af1bf2b4b fix(chassis): legacy chassis keeps failure class and status, no raw exception text (RU-083; CEG-COR-087, CEG-COR-088) |
| RU-084 | R4 | LOW | READY | 043 | DONE | `inference_geo_usps_zones_enabled` | 8ca90d1cf4 fix(inference): USPS first-digit zone mapping and country guard in infer_geography_from_postal_code (RU-084; CEG-COR-043) |
| RU-085 | R4 | LOW | READY | 091, 092 | DONE | — | fadedb6a5b fix(chassis): AuditLogger single lock, atomic buffer swap, per-sink retry, enforced buffer_size (RU-085; CEG-COR-091) |
| RU-086 | R4 | LOW | READY | 093 | DONE | `packet_audit_on_cancel_enabled` | ad5fe7f8cf fix(chassis): SDK audit wrapper records a failed pair when the handler is cancelled on timeout (RU-086; CEG-COR-093) |
| RU-087 | R4 | LOW | READY | 094 | DONE | — | 22915282fb fix(l9_core): PacketEnvelope verifies supplied content_hash and freezes payload (RU-087; CEG-COR-094) |
| RU-088 | R4 | LOW | READY | 097 | DONE | `inference_persona_token_match_enabled` | 633e9f50d0 fix(inference): token-based keyword matching in infer_buyer_persona (RU-088; CEG-COR-097) |
| RU-089 | R4 | LOW | READY | 098 | DONE | `inference_input_parsing_strict_enabled` | 4ba10dbf55 fix(inference): per-field input parsing in company-size and email-domain rules (RU-089; CEG-COR-098) |
| RU-090 | R4 | LOW | READY | 099 | DONE | `capability_record_binding_enabled` | ba463f7512 fix(auth): CapabilityValidator binds validation to the registered record and its expiry (RU-090; CEG-COR-099) |
| RU-091 | R4 | LOW | READY | 100 | DONE | — | 6bc426c5aa fix(packet): build_request_packet stops advertising parameters it cannot honour (RU-091; CEG-COR-100) |
| RU-092 | R4 | LOW | GATED (DEC-34) | 173 | DONE | — | 46597157f6 fix(packet): legacy PacketEnvelope.from_wire verifies integrity; content_hash doc aligned to code (RU-092; CEG-COR-173) |
| RU-093 | R5 | MEDIUM | READY | 034 | DONE | `neo4j_circuit_ignore_client_errors` | 505c89a593 fix(graph): client/semantic Neo4j errors do not trip the shared circuit breaker (RU-093; CEG-COR-034) |
| RU-094 | R5 | HIGH | READY | 035 | DONE | `neo4j_circuit_release_cancelled_probes` | 3ea148e6fc fix(graph): release HALF_OPEN probe slots on cancellation so the breaker cannot wedge (RU-094; CEG-COR-035) |
| RU-095 | R5 | LOW | READY | 170 | DONE | `neo4j_provision_inside_breaker` | 0e850b9bec fix(graph): admit through the circuit breaker before first-use database provisioning (RU-095; CEG-COR-170) |
| RU-096 | R5 | MEDIUM | READY | 036 | DONE | `gds_dag_job_identity_by_name` | f05d3394fe fix(gds): key the GDS execution DAG by job, not algorithm (RU-096; CEG-COR-036) |
| RU-097 | R5 | MEDIUM | READY | 037 | DONE | `gds_temporal_recency_decay_existing` | 1b932fb16c fix(gds): temporal-recency overlay aggregates per pair and decays existing edges every run (RU-097; CEG-COR-037) |
| RU-098 | R5 | LOW | READY | 168 | DONE | `gds_cooccurrence_deterministic_prune` | 4cd35c3b53 fix(gds): deterministic, weight-ordered co-occurrence pruning that never drops a hub below its keep count (RU-098; CEG-COR-168) |
| RU-099 | R5 | LOW | GATED (DEC-39) | 085 | DONE | — | e0a38d11ee fix(gds): stable Louvain states its real contract (RU-099; CEG-COR-085) |
| RU-100 | R5 | LOW | READY | 086 | DONE | `gds_history_utc_timestamps` | 3d87c32201 fix(gds): record GDS job history in timezone-aware UTC (RU-100; CEG-COR-086) |
| RU-101 | R5 | MEDIUM | READY | 059, 060, 061 | DONE | `entity_resolution_merge_integrity` | 6c98197bb5 fix(resolution): entity resolution merge integrity — threshold threading, canonical-relative verification, idempotent edges, batch skip by entity id (RU-101; CEG-COR-059, CEG-COR-060, CEG-COR-061) |
| RU-102 | R5 | LOW | GATED (DEC-40) | 124 | DONE | — | 620fe9f548 fix(resolution): entity-resolution contract states linkage only (RU-102; CEG-COR-124) |
| RU-103 | R5 | LOW | READY | 177 | DONE | `resolution_behavioral_label_scoped` | c76e203043 fix(resolution): scope behavioral similarity to the entity label and its declared id (RU-103; CEG-COR-177) |
| RU-104 | R5 | LOW | READY | 127 | DONE | — | 157a8a35d4 fix(diagnostics): align fingerprint vectors over a shared key set before distance (RU-104; CEG-COR-127) |
| RU-105 | R5 | LOW | READY | 128 | DONE | — | f302a78f3f fix(shadow): report non-finite scores and duplicate candidate ids in shadow comparison (RU-105; CEG-COR-128) |
| RU-106 | R5 | LOW | READY | 129 | DONE | — | 711a9e43a4 fix(replay): total event order so outcome_set_hash is permutation-invariant (RU-106; CEG-COR-129) |
| RU-107 | R5 | LOW | READY | 130 | DONE | — | 4621ea4c4b fix(arbitration): ArbitrationInput range check is an inclusion test that rejects NaN (RU-107; CEG-COR-130) |
| RU-108 | R6 | MEDIUM | READY | 044 | DONE | `health_absent_confidence_counts_enabled` | a51a22f086 fix(health): absent confidence must not zero readiness; forward confidences on every health path (RU-108; CEG-COR-044) |
| RU-109 | R6 | MEDIUM | READY | 045 | DONE | `health_range_gate_fields_enabled` | a9a575cbf0 fix(health): gate-critical set includes range/window bound properties (RU-109; CEG-COR-045) |
| RU-110 | R6 | MEDIUM | READY | 046 | DONE | `health_blocked_score_scale_enabled` | 37e4753d40 fix(health): blocked readiness score on the same 60% gate scale (RU-110; CEG-COR-046) |
| RU-111 | R6 | MEDIUM | READY | 047 | DONE | `health_priority_staleness_enabled` | b8930a018d fix(health): enrichment priority uses the computed staleness penalty (RU-111; CEG-COR-047) |
| RU-112 | R6 | LOW | GATED (DEC-44) | 048 | DONE | — | 8142fb6439 fix(kge): rank-aggregation fused score depends on component scores (RU-112; CEG-COR-048) |
| RU-113 | R6 | LOW | READY | 049 | DONE | — | 0d63fb3bbf fix(kge): CompoundE3D training descends the margin loss (RU-113; CEG-COR-049) |
| RU-114 | R6 | LOW | READY | 101 | DONE | — | e7514a2c11 fix(health): nightly scan de-dup state local to each run (RU-114; CEG-COR-101) |
| RU-115 | R6 | LOW | READY | 102 | DONE | — | 987a4cdb66 fix(health): nightly scan counters partition the assessed set (RU-115; CEG-COR-102) |
| RU-116 | R6 | LOW | READY | 103 | DONE | — | 9696ab15df fix(intake): proposals derive gate/scoring metadata from the spec for every mapping (RU-116; CEG-COR-103) |
| RU-117 | R6 | LOW | READY | 104 | DONE | — | 6624251389 fix(intake): impact coverage uses one counting unit (RU-117; CEG-COR-104) |
| RU-118 | R6 | LOW | READY | 105 | DONE | — | 6a30c143c3 fix(intake): intake handlers bind payload.domain to the packet tenant (RU-118; CEG-COR-105) |
| RU-119 | R6 | LOW | GATED (DEC-45) | 106 | DONE | — | 730142a1e8 fix(kge): build and train CompoundE3D operator cascades (RU-119; CEG-COR-106) |
| RU-120 | R6 | LOW | READY | 107 | DONE | — | d2543047ec fix(kge): Shear.inverse returns the true inverse (RU-120; CEG-COR-107) |
| RU-121 | R6 | LOW | READY | 108 | DONE | — | 446f7d8753 fix(kge): beam search scores composed cascades (RU-121; CEG-COR-108); 3dd2dd51d9 fix(kge): validate and diversity-prune complete cascades (RU-121/RU-122 follow-up) |
| RU-122 | R6 | LOW | READY | 109 | DONE | — | fce0a914cd fix(kge): diversity pruning only compares same-type transformations (RU-122; CEG-COR-109); 3dd2dd51d9 fix(kge): validate and diversity-prune complete cascades (RU-121/RU-122 follow-up) |
| RU-123 | R6 | LOW | READY | 110 | DONE | — | 059771be48 fix(kge): BeamSearchConfig default width follows C-20 (RU-123; CEG-COR-110) |
| RU-124 | R6 | LOW | GATED (DEC-46) | 111 | DONE | — | 04f82eea7b fix(kge): ensemble strategy resolution fails loudly; contract spelling aligned (RU-124; CEG-COR-111) |
| RU-125 | R6 | LOW | READY | 112 | DONE | — | d3e461177e fix(kge): score_against_icp does not mutate the entity table (RU-125; CEG-COR-112) |
| RU-126 | R6 | LOW | READY | 113 | DONE | — | 3d2cd26b97 fix(kge): train() does not reorder the caller's triples (RU-126; CEG-COR-113) |
| RU-127 | R6 | LOW | READY | 114 | DONE | — | 941e357ba8 fix(personas): composite persona falls back to equal weights on all-zero scores (RU-127; CEG-COR-114) |
| RU-128 | R6 | LOW | READY | 115 | DONE | — | 877dca05d6 fix(personas): synthesis weights respect the minimum floor after normalisation (RU-128; CEG-COR-115) |
| RU-129 | R6 | LOW | READY | 176 | DONE | — | abef24743f fix(hoprag): HopRAGConfig rejects index_batch_size < 1 (RU-129; CEG-COR-176) |
| RU-130 | R7 | HIGH | READY | 010, 065, 066, 138 | DONE | — | 3f94a39ab4 fix(tools): make l9_meta_injector injection non-destructive and idempotent (RU-130; CEG-COR-010) |
| RU-131 | R7 | MEDIUM | GATED (DEC-49) | 067 | DONE | — | 4b39caae0c chore(headers): normalise L9_META headers and align C-18 texts (RU-131 part 1/2; CEG-COR-067); 44508943db fix(tools): make the C-18 header verification able to fail, over the contract's file set (RU-131; CEG-COR-067); 786d3a2772 fix(tools): register the moved chassis auth test at its new path (RU-082 x RU-131); 137fa8448a fix(tools): drop the registry entry for the retired domain_extractor (RU-054 x RU-131); 0c184917c2 test(tools): retired domain_extractor stays unregistered (RU-054 x RU-131) |
| RU-132 | R7 | MEDIUM | GATED (DEC-50) | 064 | DONE | — | 03f93b6244 fix(tools): make the architecture audit cover all of engine/ without SEC_LABEL false positives (RU-132; CEG-COR-064) |
| RU-133 | R7 | LOW | READY | 135 | DONE | — | 3d8154b02a fix(tools): audit_harness reports infrastructure errors instead of passing them (RU-133; CEG-COR-135) |
| RU-134 | R7 | MEDIUM | READY | 068 | DONE | — | 971bde81a5 fix(tools): scope spec_extract evidence to the declared search_files (RU-134; CEG-COR-068) |
| RU-135 | R7 | LOW | READY | 139 | DONE | — | 45a5ce8a2a fix(tools): count spec_extract research features once (RU-135; CEG-COR-139) |
| RU-136 | R7 | LOW | GATED (DEC-51) | 131 | DONE | — | b35a5ec376 fix(tools): make ERR-002 able to fire, enforced in engine/ (RU-136; CEG-COR-131) |
| RU-137 | R7 | LOW | READY | 132, 133 | DONE | — | 885e348c58 fix(tools): evaluate SEC-007, ARCH-001/002, DEL-001/002 on the AST (RU-137; CEG-COR-132) |
| RU-138 | R7 | LOW | READY | 134 | DONE | — | 91368e3c6f fix(tools): close cypher_lint (C-009) false negatives (RU-138; CEG-COR-134); 62afe60326 fix(tools): cypher_lint skips query-shaped messages that only reach an exception (RU-138 follow-up) |
| RU-139 | R7 | LOW | READY | 136 | DONE | — | 807b8cea95 fix(tools): correct static auditor classification and audit_dispatch --strict exit (RU-139; CEG-COR-136) |
| RU-140 | R7 | LOW | READY | 137 | DONE | — | 0c9b916b86 fix(tools): api_regression detects removed modules and diffs against the merge base (RU-140; CEG-COR-137) |
| RU-141 | R7 | LOW | READY | 140 | DONE | — | 0049b46fd5 fix(tools): pytest-unit hook ignores non-test helpers under tests/ (RU-141; CEG-COR-140) |
| RU-142 | R7 | LOW | READY | 141 | DONE | — | 9fb5566491 fix(scripts): restore comment markers and shebangs in the 10 corrupted template scripts (RU-142; CEG-COR-141) |
| RU-143 | R7 | LOW | READY | 142 | DONE | — | 7a6f9e1dac fix(tools): group the repo-root fallback so it runs only when git fails (RU-143; CEG-COR-142) |
| RU-144 | R7 | LOW | READY | 143 | DONE | — | 1917271ed4 fix(tools): mask short secret values fully in check_env (RU-144; CEG-COR-143) |
| RU-145 | R7 | LOW | READY | 144 | DONE | — | 067fb6fc87 fix(tools): propagate build-context errors in docker_validate.sh (RU-145; CEG-COR-144) |
| RU-146 | R7 | LOW | READY | 145 | DONE | — | 88ca5c30b6 fix(tools): use pytest's exit status for the smoke verdict in test_everything.sh (RU-146; CEG-COR-145) |
| RU-147 | R7 | LOW | READY | 146 | DONE | — | 55f0daa772 fix(tools): fix precommit_smoke health parsing and stop masking pytest failures (RU-147; CEG-COR-146) |
| RU-148 | R7 | LOW | READY | 147 | DONE | — | 19c06092a5 fix(tools): fix deep_mri.sh summary substitution and health count (RU-148; CEG-COR-147) |
| RU-149 | R7 | LOW | GATED (DEC-52) | 148 | DONE | — | 6d253d30e8 fix(scripts): align template seed/gds/dev scripts with the live ingress and propagate failures (RU-149; CEG-COR-148); 72c767c2d8 test(scripts): rename the stub-curl log variable in the RU-149 tests (RU-149; CEG-COR-148) |
| RU-150 | R7 | LOW | READY | 149 | DONE | — | 51bb611799 fix(scripts): entrypoint banner reports the effective chassis default (RU-150; CEG-COR-149) |
| RU-151 | R8 | MEDIUM | READY | 073 | DONE_THEN_RETIRED | — | 130f8f8 removed the cleartext-bearer default and enabled TLS verification; the module was then deleted by DEC-53 RETIRE (7279b35). DEC-56 (rotate MCP_API_KEY_C) remains a human action. |
| RU-152 | R8 | LOW | GATED (DEC-53) | 072 | RETIRED | — | DEC-53 RETIRE: agents/cursor Python tooling deleted in 7279b35 (no importer in engine/chassis/tools, not packaged, not in any image or workflow; import failed with ModuleNotFoundError 'core'). Keep-branch repair not applicable. |
| RU-153 | R8 | LOW | GATED (DEC-53,DEC-54) | 156 | RETIRED | — | DEC-53 RETIRE (7279b35); DEC-54 moot. |
| RU-154 | R8 | LOW | GATED (DEC-53) | 155 | RETIRED | — | DEC-53 RETIRE (7279b35). |
| RU-155 | R8 | LOW | GATED (DEC-53) | 154 | RETIRED | — | DEC-53 RETIRE (7279b35): the keep branch (importability) no longer exists. |
| RU-156 | R8 | LOW | GATED (DEC-53,DEC-55) | 157 | RETIRED | — | DEC-53 RETIRE (7279b35); DEC-55 moot. |
| RU-157 | R8 | LOW | GATED (DEC-53) | 158 | RETIRED | — | DEC-53 RETIRE (7279b35). |
| RU-158 | R8 | LOW | GATED (DEC-53) | 159 | RETIRED | — | DEC-53 RETIRE (7279b35). |
| RU-159 | R8 | LOW | GATED (DEC-53) | 160 | RETIRED | — | DEC-53 RETIRE (7279b35). |
| RU-160 | R8 | LOW | GATED (DEC-53) | 161 | RETIRED | — | DEC-53 RETIRE (7279b35). |
| RU-161 | R8 | LOW | GATED (DEC-53) | 162 | RETIRED | — | DEC-53 RETIRE (7279b35). |
| RU-162 | R8 | LOW | GATED (DEC-53) | 163 | RETIRED | — | DEC-53 RETIRE (7279b35). |

## Decisions

| Decision | Blocks | Selected | Rationale | Verification |
|---|---|---|---|---|
| DEC-01 | — | Tenant-scoped behind admin_tenant_bound_domain, consistent with DOMAIN_SPEC_VERSIONING.md:84-87. |  |  |
| DEC-02 (blocking) | RU-006 | First alternative; handle_outcomes remains the canonical writer (established, majority of readers). | handle_outcomes is the established writer with the majority of readers (rule 91: established code wins). | RU-006: sync outcome now writes the same :TransactionOutcome/success\|failure\|partial record via one shared writer; score_feedback/discover_weights read it (tests/unit/test_outcomes.py, test_wave2_feedback.py). |
| DEC-03 (blocking) | RU-010 | Third alternative (reject undeclared or required params; optional params follow the gate's nullbehavior) — needs owner confirmation. | Matches the W1-03 contract text (reject null required/undeclared) without breaking optional params, which keep their declared nullbehavior. | RU-010: validate_gates(reject_unbound=True) in handle_match; fixtures that omitted required fields were the callers' defect and now supply them. Rollback STRICT_NULL_GATES=false (verified in the flag-OFF run). |
| DEC-04 (blocking) | RU-012 | Admit both behind match_request_contract_admission. | Gate/Odoo producers emit MatchRequest; legacy callers stay intact. | RU-012 behind match_request_contract_admission; the contract example's facts aligned to the plasticos query vocabulary (31ef14a, C-12) so the verbatim example returns candidates under default flags. |
| DEC-05 (blocking) | RU-015 | Withhold via admission_defects until a domain needs roles. | No shipped pack declares roleexempt/relaxedpenalty; fail-closed admission instead of plumbing an unused role. | RU-015: admission_defects names the field; no served pack affected. |
| DEC-06 | — | Reject. |  |  |
| DEC-07 | — | Spec-load validation error. |  |  |
| DEC-08 (blocking) | RU-021 | A | Option A is 'keep the unimplemented HGKR Pass-2 options fail-closed at admission (RU-020) and treat implementation as separately authorized feature work'. | RU-021 corrected the preference_attention sample expression only; the other Pass-2 options remain withheld by RU-020 admission (option B, end-to-end implementation with schema changes, is not authorized by this campaign). |
| DEC-09 | — | A (documented intent in code); revisit with C-22 owner |  |  |
| DEC-10 | — | C now; A/B as product decision |  |  |
| DEC-11 | — | A |  |  |
| DEC-12 (blocking) | RU-033 | A' | Honours the query-schema text ('when true, only active') and never empties the result on omission. | RU-033: threshold >= gate, nullbehavior fail, default true; truth table and admission_defects()==[]; integration (live Neo4j) green. |
| DEC-13 | — | A (B may follow as a readability improvement) |  |  |
| DEC-14 (blocking) | RU-037 | A, with B as an interim so freight stops failing every match | Final option A taken directly (engine geodecay binds declared longitude props), so the interim B was unnecessary. | RU-037 behind geodecay_declared_longitude: freight deadhead uses currentlat/currentlon vs pickuplat/pickuplon, rate_alignment uses declared Carrier ratepermile; fake-driver capture MISSING PARAMS []; freight served again; plasticos geodecay byte-identical on/off. |
| DEC-15 (blocking) | RU-038 | D now, then A | Owner constraint: mortgage matching must not be exposed without licensing and blacklist enforcement. 'D now' = declare the pack dormant; 'then A' (a construct that can constrain a non-candidate alias / query-anchored edge) is new gate capability and is NOT implemented here. | RU-038: lenderresolve executable, licensing step and query-anchored blacklist gate removed with decision comments, domain.activation: dormant; list_domains() refuses it with the dormant reason by default. Re-serving mortgage requires DEC-15 option A as separate feature work (external blocker). |
| DEC-16 | — | A |  |  |
| DEC-17 | — | B (no shipped pack needs list-valued query params; PropertyType has no list type) |  |  |
| DEC-18 (blocking) | RU-047 | A if callers can supply it, else B | Decided per pack on evidence: plasticos callers can supply the buyer Facility's Louvain community_id (the Louvain job writes community_id on Facility); mortgage has no producer, so its community dimension is removed. | RU-047: plasticos queryschema gains optional community_id; mortgage approval_rate binds the lender alias; RG-061 reports no unbound dimension. |
| DEC-19 (blocking) | RU-054 | A | The consolidated master spec and its extractor were a destructive, unused workflow. | RU-054: domains/MASTER-SPEC-ALL-DOMAINS.yaml and tools/domain_extractor.py deleted with their lint/registry residue; test asserts no remaining reference. |
| DEC-20 (blocking) | RU-058 | A | Express 'below maximum' with an ingested remaining-capacity property and the existing threshold gate (no new gate semantics). | RU-058: dormant packs use comparable operands; 5/10 admitted, 10/10 rejected. |
| DEC-21 | — | Confirm with the data owner; default to A if ingestion stores 0-100 |  |  |
| DEC-22 | — | (a) |  |  |
| DEC-23 | — | (a) |  |  |
| DEC-24 | — | (c) |  |  |
| DEC-25 | — | (a) |  |  |
| DEC-26 | — | (a) |  |  |
| DEC-27 | — | (a) |  |  |
| DEC-28 | — | (a) |  |  |
| DEC-29 (blocking) | RU-082, RU-083 | Keep as dev/test-only and apply the small repairs. Retirement is a separate architectural change. | Keep the legacy dict chassis as dev/test-only; retirement is a separate architectural change. | RU-082/RU-083: factory refuses non-dev environments; status classes preserved; raw exception text never echoed. |
| DEC-30 (blocking) | RU-082 | Option 1, together with the unconditional factory env guard | Authenticate when L9_API_KEY is set, plus an unconditional factory env guard. | RU-082 behind legacy_chassis_auth_enabled: 401/403 cases, tenant check, prod factory raises. |
| DEC-31 (blocking) | RU-081 | Option 1 (existing documented contract wins; column grant bounds the relaxation) | The documented outcome contract wins; a column-level UPDATE grant bounds the relaxation of packet immutability. | RU-081 proved on PostgreSQL 16: column + GRANT UPDATE(outcome_jsonb) + tenant-scoped FOR UPDATE policy -> record_outcome True as owner and as l9_product; envelope update refused; other tenant False. |
| DEC-32 | — | l9_product login role in production, decided with infra owners. Not required for any R4 RU to land. |  |  |
| DEC-33 | — | Add it as a separate infra-approved change. R4-RU-01..04 land with unit tests and manual repro evidence meanwhile. |  |  |
| DEC-34 (blocking) | RU-092 | Align doc to code | The code formula is what every stored hash used; changing code would invalidate existing hashes. | RU-092: PACKET_ENVELOPE_FIELDS.md formula rewritten to the code's; the doc formula reproduces the code hash in a test; from_wire verifies integrity. |
| DEC-35 | — | Keep emitting (smallest change). Revisit if domain specs start declaring country. |  |  |
| DEC-36 | — | Harden (no CI edits) |  |  |
| DEC-37 | — | Remove |  |  |
| DEC-38 | — | A now (RU-01); revisit C only if per-database outages are observed. |  |  |
| DEC-39 (blocking) | RU-099 | A | Correct the contract to what stability_runs actually does (last-run labels + averaged metrics) rather than build consensus clustering. | RU-099: docstring/contract corrected, result carries consensus: False; stability_runs=1 unchanged. |
| DEC-40 (blocking) | RU-102 | A | Option A narrows the documented contract to linkage only (no relationship transfer, no match-time RESOLVED_FROM honouring). | RU-102: graph-schema.yaml direction and docstrings aligned with what the resolver writes; contract tests added. No runtime change. |
| DEC-41 | — | A |  |  |
| DEC-42 | — | A, with explicit human approval per AGENTS.md Ask First |  |  |
| DEC-43 | — | A, behind health_absent_confidence_counts_enabled=True; R6-RU-01 also forwards an explicitly supplied map on batch/report so both alternatives' acceptance criteria hold. |  |  |
| DEC-44 (blocking) | RU-112 | Borda-weighted mean for BORDA (and the default), top score for PLURALITY. | As recommended. | RU-112: fused score depends on component scores (Borda-weighted mean; top score for PLURALITY); Pareto front no longer forced to 'borda'. |
| DEC-45 (blocking) | RU-119 | Human decision. Default recommendation: implement (C-20 is the governing MUST and amending it is the larger governance change); sequence after R6-RU-06. | Owner-approved: implement (C-20 is the governing MUST). | RU-119: per-relation head cascades [Rotation, Scale] built from identity, trained jointly by backprop through the cascade (gradients checked against finite differences); reuses the RU-121 _CascadeTransformation; KGE stays dormant (kge_enabled=False). |
| DEC-46 (blocking) | RU-124 | Option 1. | Code spellings (FusionStrategy values) are authoritative; unknown/unimplemented strategies fail loudly. | RU-124: default_strategy/predict raise naming accepted values; KGE_EMBEDDINGS.md uses code spellings; supersedes RU-136's log-and-default in the same function (b35a5ec). |
| DEC-47 | — | Local fixes now; consolidation only if separately approved. |  |  |
| DEC-48 | — | skipped. |  |  |
| DEC-49 (blocking) | RU-131 | B — smallest change that makes the governing text match the only tool that exists; reconsider A as a separate feature if owner-less v2 is still wanted. | Make the governing C-18 text describe the only tool that exists (v1 injector). | RU-131: check exits 1 on any finding over the contract file set; 38 headers normalised; merged tree check PASS (after the RU-082/RU-054 registry reconciliations 786d3a2/137fa84). |
| DEC-50 (blocking) | RU-132 | A | Delegate SEC_LABEL to cypher_lint (the C-009 owner) when the glob becomes recursive. | RU-132: audit covers all of engine/; CRITICAL/HIGH 0 before and after on the real tree. |
| DEC-51 (blocking) | RU-136 | Enforce in engine/ only with explicit, reasoned exemptions for intentional cases (boot.py:217 CancelledError on shutdown), since ERROR_HANDLING.md is the contract doc for engine code. | ERROR_HANDLING.md is the engine contract; enforce in engine/ with reasoned exemptions. | RU-136: AST ERR-002 with three named exemptions; contract_scanner exits 0. |
| DEC-52 (blocking) | RU-149 | Remove 'all' (require job_name) unless gds_status is verified to list job names. | gds_status lists run history, not job names, so 'all' cannot be implemented honestly. | RU-149: job_name required; seed/gds/dev scripts target the live ingress and propagate failures. |
| DEC-53 (blocking) | RU-152, RU-153, RU-154, RU-155, RU-156, RU-157, RU-158, RU-159, RU-160, RU-161, RU-162 | RETIRE (delete the Python tooling and its test/config residue) — owner confirmation required. Land R8-RU-01 immediately regardless. | Conditional retirement: verified no required live consumer before deletion. | No importer in engine/chassis/tools/tests (other than its own test), not in pyproject packages, Dockerfiles, Makefile or workflows; `import agents.cursor` already failed (ModuleNotFoundError 'core'). Deleted in 7279b35 with config residue; Markdown/YAML under agents/cursor kept (governance rule 87 still names cursor_memory_kernel.yaml). |
| DEC-54 (blocking) | RU-153 | Remove the option unless the owner supplies the server's save_memory schema. | Moot under DEC-53 RETIRE. | RU-153 retired. |
| DEC-55 (blocking) | RU-156 | Deny unknown features. | Moot under DEC-53 RETIRE. | RU-156 retired. |
| DEC-56 | — | Rotate (or confirm the key is already revoked with the C1 decommission). |  |  |

## Findings

| Outcome | Count |
|---|---|
| DONE | 161 |
| DONE_THEN_RETIRED | 1 |
| DUPLICATE | 1 |
| INVALID | 57 |
| MOOT_RETIRED | 1 |
| RETIRED | 11 |

| Finding | Audit sev | Plan disposition | Unit | Outcome | Note |
|---|---|---|---|---|---|
| CEG-COR-001 | HIGH | CONFIRMED_ACTIVE | RU-001 | DONE | via RU-001 |
| CEG-COR-002 | HIGH | CONFIRMED_ACTIVE | RU-002 | DONE | via RU-002 |
| CEG-COR-003 | HIGH | CONFIRMED_ACTIVE | RU-003 | DONE | via RU-003 |
| CEG-COR-004 | HIGH | CONFIRMED_ACTIVE | RU-033 | DONE | via RU-033 |
| CEG-COR-005 | HIGH | CONFIRMED_ACTIVE | RU-034 | DONE | via RU-034 |
| CEG-COR-006 | HIGH | CONFIRMED_ACTIVE | RU-017 | DONE | via RU-017 |
| CEG-COR-007 | HIGH | CONFIRMED_ACTIVE | RU-036 | DONE | via RU-036 |
| CEG-COR-008 | HIGH | CONFIRMED_ACTIVE | RU-061 | DONE | via RU-061 |
| CEG-COR-009 | HIGH | CONFIRMED_ACTIVE | RU-062 | DONE | via RU-062 |
| CEG-COR-010 | HIGH | CONFIRMED_ACTIVE | RU-130 | DONE | via RU-130 |
| CEG-COR-011 | HIGH | CONFIRMED_ACTIVE | RU-037 | DONE | via RU-037 |
| CEG-COR-012 | HIGH | CONFIRMED_ACTIVE | RU-038 | DONE | via RU-038 |
| CEG-COR-013 | HIGH | CONFIRMED_ACTIVE | RU-040 | DONE | via RU-040 |
| CEG-COR-014 | MEDIUM | CONFIRMED_ACTIVE | RU-004 | DONE | via RU-004 |
| CEG-COR-015 | MEDIUM | CONFIRMED_ACTIVE | RU-006 | DONE | via RU-006 |
| CEG-COR-016 | MEDIUM | CONFIRMED_ACTIVE | RU-007 | DONE | via RU-007 |
| CEG-COR-017 | MEDIUM | CONFIRMED_ACTIVE | RU-008 | DONE | via RU-008 |
| CEG-COR-018 | MEDIUM | CONFIRMED_ACTIVE | RU-009 | DONE | via RU-009 |
| CEG-COR-019 | MEDIUM | CONFIRMED_ACTIVE | RU-041 | DONE | via RU-041 |
| CEG-COR-020 | MEDIUM | CONFIRMED_ACTIVE | RU-042 | DONE | via RU-042 |
| CEG-COR-021 | MEDIUM | CONFIRMED_ACTIVE | RU-042 | DONE | via RU-042 |
| CEG-COR-022 | MEDIUM | CONFIRMED_ACTIVE | RU-043 | DONE | via RU-043 |
| CEG-COR-023 | MEDIUM | CONFIRMED_ACTIVE | RU-010 | DONE | via RU-010 |
| CEG-COR-024 | MEDIUM | CONFIRMED_ACTIVE | RU-044 | DONE | via RU-044 |
| CEG-COR-025 | MEDIUM | CONFIRMED_ACTIVE | RU-018 | DONE | via RU-018 |
| CEG-COR-026 | MEDIUM | CONFIRMED_ACTIVE | RU-019 | DONE | via RU-019 |
| CEG-COR-027 | MEDIUM | CONFIRMED_ACTIVE | RU-020 | DONE | via RU-020 |
| CEG-COR-028 | MEDIUM | CONFIRMED_ACTIVE | RU-020 | DONE | via RU-020 |
| CEG-COR-029 | MEDIUM | CONFIRMED_ACTIVE | RU-020 | DONE | via RU-020 |
| CEG-COR-030 | MEDIUM | CONFIRMED_ACTIVE | RU-022 | DONE | via RU-022 |
| CEG-COR-031 | MEDIUM | CONFIRMED_ACTIVE | RU-024 | DONE | via RU-024 |
| CEG-COR-032 | MEDIUM | CONFIRMED_ACTIVE | RU-025 | DONE | via RU-025 |
| CEG-COR-033 | MEDIUM | CONFIRMED_ACTIVE | RU-011 | DONE | via RU-011 |
| CEG-COR-034 | MEDIUM | CONFIRMED_ACTIVE | RU-093 | DONE | via RU-093 |
| CEG-COR-035 | MEDIUM | CONFIRMED_ACTIVE | RU-094 | DONE | via RU-094 |
| CEG-COR-036 | MEDIUM | CONFIRMED_ACTIVE | RU-096 | DONE | via RU-096 |
| CEG-COR-037 | MEDIUM | CONFIRMED_ACTIVE | RU-097 | DONE | via RU-097 |
| CEG-COR-038 | MEDIUM | CONFIRMED_ACTIVE | RU-045 | DONE | via RU-045 |
| CEG-COR-039 | MEDIUM | CONFIRMED_ACTIVE | RU-082 | DONE | via RU-082 |
| CEG-COR-040 | MEDIUM | CONFIRMED_ACTIVE | RU-078 | DONE | via RU-078 |
| CEG-COR-041 | MEDIUM | CONFIRMED_ACTIVE | RU-081 | DONE | via RU-081 |
| CEG-COR-042 | MEDIUM | CONFIRMED_ACTIVE | RU-079 | DONE | via RU-079 |
| CEG-COR-043 | MEDIUM | CONFIRMED_ACTIVE | RU-084 | DONE | via RU-084 |
| CEG-COR-044 | MEDIUM | CONFIRMED_ACTIVE | RU-108 | DONE | via RU-108 |
| CEG-COR-045 | MEDIUM | CONFIRMED_ACTIVE | RU-109 | DONE | via RU-109 |
| CEG-COR-046 | MEDIUM | CONFIRMED_ACTIVE | RU-110 | DONE | via RU-110 |
| CEG-COR-047 | MEDIUM | CONFIRMED_ACTIVE | RU-111 | DONE | via RU-111 |
| CEG-COR-048 | MEDIUM | CONFIRMED_ACTIVE | RU-112 | DONE | via RU-112 |
| CEG-COR-049 | MEDIUM | CONFIRMED_ACTIVE | RU-113 | DONE | via RU-113 |
| CEG-COR-050 | MEDIUM | CONFIRMED_ACTIVE | RU-063 | DONE | via RU-063 |
| CEG-COR-051 | MEDIUM | CONFIRMED_ACTIVE | RU-064 | DONE | via RU-064 |
| CEG-COR-052 | MEDIUM | CONFIRMED_ACTIVE | RU-065 | DONE | via RU-065 |
| CEG-COR-053 | MEDIUM | CONFIRMED_ACTIVE | RU-065 | DONE | via RU-065 |
| CEG-COR-054 | MEDIUM | CONFIRMED_ACTIVE | RU-066 | DONE | via RU-066 |
| CEG-COR-055 | MEDIUM | CONFIRMED_ACTIVE | RU-067 | DONE | via RU-067 |
| CEG-COR-056 | MEDIUM | CONFIRMED_ACTIVE | RU-005 | DONE | via RU-005 |
| CEG-COR-057 | MEDIUM | CONFIRMED_ACTIVE | RU-068 | DONE | via RU-068 |
| CEG-COR-058 | MEDIUM | CONFIRMED_ACTIVE | RU-069 | DONE | via RU-069 |
| CEG-COR-059 | MEDIUM | CONFIRMED_ACTIVE | RU-101 | DONE | via RU-101 |
| CEG-COR-060 | MEDIUM | CONFIRMED_ACTIVE | RU-101 | DONE | via RU-101 |
| CEG-COR-061 | MEDIUM | CONFIRMED_ACTIVE | RU-101 | DONE | via RU-101 |
| CEG-COR-062 | MEDIUM | CONFIRMED_ACTIVE | RU-070 | DONE | via RU-070 |
| CEG-COR-063 | MEDIUM | CONFIRMED_ACTIVE | RU-012 | DONE | via RU-012 |
| CEG-COR-064 | MEDIUM | CONFIRMED_ACTIVE | RU-132 | DONE | via RU-132 |
| CEG-COR-065 | MEDIUM | CONFIRMED_ACTIVE | RU-130 | DONE | via RU-130 |
| CEG-COR-066 | MEDIUM | CONFIRMED_ACTIVE | RU-130 | DONE | via RU-130 |
| CEG-COR-067 | MEDIUM | CONFIRMED_ACTIVE | RU-131 | DONE | via RU-131 |
| CEG-COR-068 | MEDIUM | CONFIRMED_ACTIVE | RU-134 | DONE | via RU-134 |
| CEG-COR-069 | MEDIUM | CONFIRMED_ACTIVE | RU-046 | DONE | via RU-046 |
| CEG-COR-070 | MEDIUM | CONFIRMED_ACTIVE | RU-047 | DONE | via RU-047 |
| CEG-COR-071 | MEDIUM | CONFIRMED_ACTIVE | RU-048 | DONE | via RU-048 |
| CEG-COR-072 | MEDIUM | CONFIRMED_ACTIVE | RU-152 | RETIRED | via RU-152; DEC-53 RETIRE (7279b35) |
| CEG-COR-073 | MEDIUM | CONFIRMED_ACTIVE | RU-151 | DONE_THEN_RETIRED | via RU-151 (130f8f8), module then deleted by DEC-53 (7279b35) |
| CEG-COR-074 | LOW | CONFIRMED_ACTIVE | RU-013 | DONE | via RU-013 |
| CEG-COR-075 | LOW | CONFIRMED_ACTIVE | RU-014 | DONE | via RU-014 |
| CEG-COR-076 | LOW | CONFIRMED_ACTIVE | RU-049 | DONE | via RU-049 |
| CEG-COR-077 | LOW | CONFIRMED_ACTIVE | RU-050 | DONE | via RU-050 |
| CEG-COR-078 | LOW | CONFIRMED_ACTIVE | RU-051 | DONE | via RU-051 |
| CEG-COR-079 | LOW | CONFIRMED_ACTIVE | RU-052 | DONE | via RU-052 |
| CEG-COR-080 | LOW | CONFIRMED_ACTIVE | RU-015 | DONE | via RU-015 |
| CEG-COR-081 | LOW | CONFIRMED_ACTIVE | RU-053 | DONE | via RU-053 |
| CEG-COR-082 | LOW | CONFIRMED_ACTIVE | RU-021 | DONE | via RU-021 |
| CEG-COR-083 | LOW | CONFIRMED_ACTIVE | RU-023 | DONE | via RU-023 |
| CEG-COR-084 | LOW | CONFIRMED_ACTIVE | RU-026 | DONE | via RU-026 |
| CEG-COR-085 | LOW | CONFIRMED_ACTIVE | RU-099 | DONE | via RU-099 |
| CEG-COR-086 | LOW | CONFIRMED_ACTIVE | RU-100 | DONE | via RU-100 |
| CEG-COR-087 | LOW | CONFIRMED_ACTIVE | RU-083 | DONE | via RU-083 |
| CEG-COR-088 | LOW | CONFIRMED_ACTIVE | RU-083 | DONE | via RU-083 |
| CEG-COR-089 | LOW | CONFIRMED_ACTIVE | RU-071 | DONE | via RU-071 |
| CEG-COR-090 | LOW | CONFIRMED_ACTIVE | RU-071 | DONE | via RU-071 |
| CEG-COR-091 | LOW | CONFIRMED_ACTIVE | RU-085 | DONE | via RU-085 |
| CEG-COR-092 | LOW | CONFIRMED_ACTIVE | RU-085 | DONE | via RU-085 |
| CEG-COR-093 | LOW | CONFIRMED_ACTIVE | RU-086 | DONE | via RU-086 |
| CEG-COR-094 | LOW | CONFIRMED_ACTIVE | RU-087 | DONE | via RU-087 |
| CEG-COR-095 | LOW | CONFIRMED_ACTIVE | RU-078 | DONE | via RU-078 |
| CEG-COR-096 | LOW | CONFIRMED_ACTIVE | RU-080 | DONE | via RU-080 |
| CEG-COR-097 | LOW | CONFIRMED_ACTIVE | RU-088 | DONE | via RU-088 |
| CEG-COR-098 | LOW | CONFIRMED_ACTIVE | RU-089 | DONE | via RU-089 |
| CEG-COR-099 | LOW | CONFIRMED_ACTIVE | RU-090 | DONE | via RU-090 |
| CEG-COR-100 | LOW | CONFIRMED_ACTIVE | RU-091 | DONE | via RU-091 |
| CEG-COR-101 | LOW | CONFIRMED_ACTIVE | RU-114 | DONE | via RU-114 |
| CEG-COR-102 | LOW | CONFIRMED_ACTIVE | RU-115 | DONE | via RU-115 |
| CEG-COR-103 | LOW | CONFIRMED_ACTIVE | RU-116 | DONE | via RU-116 |
| CEG-COR-104 | LOW | CONFIRMED_ACTIVE | RU-117 | DONE | via RU-117 |
| CEG-COR-105 | LOW | CONFIRMED_ACTIVE | RU-118 | DONE | via RU-118 |
| CEG-COR-106 | LOW | CONFIRMED_ACTIVE | RU-119 | DONE | via RU-119 |
| CEG-COR-107 | LOW | CONFIRMED_ACTIVE | RU-120 | DONE | via RU-120 |
| CEG-COR-108 | LOW | CONFIRMED_ACTIVE | RU-121 | DONE | via RU-121 |
| CEG-COR-109 | LOW | CONFIRMED_ACTIVE | RU-122 | DONE | via RU-122 |
| CEG-COR-110 | LOW | CONFIRMED_ACTIVE | RU-123 | DONE | via RU-123 |
| CEG-COR-111 | LOW | CONFIRMED_ACTIVE | RU-124 | DONE | via RU-124 |
| CEG-COR-112 | LOW | CONFIRMED_ACTIVE | RU-125 | DONE | via RU-125 |
| CEG-COR-113 | LOW | CONFIRMED_ACTIVE | RU-126 | DONE | via RU-126 |
| CEG-COR-114 | LOW | CONFIRMED_ACTIVE | RU-127 | DONE | via RU-127 |
| CEG-COR-115 | LOW | CONFIRMED_ACTIVE | RU-128 | DONE | via RU-128 |
| CEG-COR-116 | LOW | CONFIRMED_ACTIVE | RU-027 | DONE | via RU-027 |
| CEG-COR-117 | LOW | CONFIRMED_ACTIVE | RU-028 | DONE | via RU-028 |
| CEG-COR-118 | LOW | CONFIRMED_ACTIVE | RU-029 | DONE | via RU-029 |
| CEG-COR-119 | LOW | CONFIRMED_ACTIVE | RU-072 | DONE | via RU-072 |
| CEG-COR-120 | LOW | CONFIRMED_ACTIVE | RU-073 | DONE | via RU-073 |
| CEG-COR-121 | LOW | CONFIRMED_ACTIVE | RU-074 | DONE | via RU-074 |
| CEG-COR-122 | LOW | CONFIRMED_ACTIVE | RU-075 | DONE | via RU-075 |
| CEG-COR-123 | LOW | CONFIRMED_ACTIVE | RU-030 | DONE | via RU-030 |
| CEG-COR-124 | LOW | CONFIRMED_ACTIVE | RU-102 | DONE | via RU-102 |
| CEG-COR-125 | LOW | CONFIRMED_ACTIVE | RU-076 | DONE | via RU-076 |
| CEG-COR-126 | LOW | CONFIRMED_ACTIVE | RU-077 | DONE | via RU-077 |
| CEG-COR-127 | LOW | CONFIRMED_ACTIVE | RU-104 | DONE | via RU-104 |
| CEG-COR-128 | LOW | CONFIRMED_ACTIVE | RU-105 | DONE | via RU-105 |
| CEG-COR-129 | LOW | CONFIRMED_ACTIVE | RU-106 | DONE | via RU-106 |
| CEG-COR-130 | LOW | CONFIRMED_ACTIVE | RU-107 | DONE | via RU-107 |
| CEG-COR-131 | LOW | CONFIRMED_ACTIVE | RU-136 | DONE | via RU-136 |
| CEG-COR-132 | LOW | CONFIRMED_ACTIVE | RU-137 | DONE | via RU-137 |
| CEG-COR-133 | LOW | CONFIRMED_ACTIVE | RU-137 | DONE | via RU-137 |
| CEG-COR-134 | LOW | CONFIRMED_ACTIVE | RU-138 | DONE | via RU-138 |
| CEG-COR-135 | LOW | CONFIRMED_ACTIVE | RU-133 | DONE | via RU-133 |
| CEG-COR-136 | LOW | CONFIRMED_ACTIVE | RU-139 | DONE | via RU-139 |
| CEG-COR-137 | LOW | CONFIRMED_ACTIVE | RU-140 | DONE | via RU-140 |
| CEG-COR-138 | LOW | CONFIRMED_ACTIVE | RU-130 | DONE | via RU-130 |
| CEG-COR-139 | LOW | CONFIRMED_ACTIVE | RU-135 | DONE | via RU-135 |
| CEG-COR-140 | LOW | CONFIRMED_ACTIVE | RU-141 | DONE | via RU-141 |
| CEG-COR-141 | LOW | CONFIRMED_ACTIVE | RU-142 | DONE | via RU-142 |
| CEG-COR-142 | LOW | CONFIRMED_ACTIVE | RU-143 | DONE | via RU-143 |
| CEG-COR-143 | LOW | CONFIRMED_ACTIVE | RU-144 | DONE | via RU-144 |
| CEG-COR-144 | LOW | CONFIRMED_ACTIVE | RU-145 | DONE | via RU-145 |
| CEG-COR-145 | LOW | CONFIRMED_ACTIVE | RU-146 | DONE | via RU-146 |
| CEG-COR-146 | LOW | CONFIRMED_ACTIVE | RU-147 | DONE | via RU-147 |
| CEG-COR-147 | LOW | CONFIRMED_ACTIVE | RU-148 | DONE | via RU-148 |
| CEG-COR-148 | LOW | CONFIRMED_ACTIVE | RU-149 | DONE | via RU-149 |
| CEG-COR-149 | LOW | CONFIRMED_ACTIVE | RU-150 | DONE | via RU-150 |
| CEG-COR-150 | LOW | CONFIRMED_ACTIVE | RU-054 | DONE | via RU-054 |
| CEG-COR-151 | LOW | CONFIRMED_ACTIVE | RU-055 | DONE | via RU-055 |
| CEG-COR-152 | LOW | CONFIRMED_ACTIVE | RU-056 | DONE | via RU-056 |
| CEG-COR-153 | LOW | CONFIRMED_ACTIVE | RU-058 | DONE | via RU-058 |
| CEG-COR-154 | LOW | CONFIRMED_ACTIVE | RU-155 | RETIRED | via RU-155; DEC-53 RETIRE (7279b35) |
| CEG-COR-155 | LOW | CONFIRMED_ACTIVE | RU-154 | RETIRED | via RU-154; DEC-53 RETIRE (7279b35) |
| CEG-COR-156 | LOW | CONFIRMED_ACTIVE | RU-153 | RETIRED | via RU-153; DEC-53 RETIRE (7279b35) |
| CEG-COR-157 | LOW | CONFIRMED_ACTIVE | RU-156 | RETIRED | via RU-156; DEC-53 RETIRE (7279b35) |
| CEG-COR-158 | LOW | CONFIRMED_ACTIVE | RU-157 | RETIRED | via RU-157; DEC-53 RETIRE (7279b35) |
| CEG-COR-159 | LOW | CONFIRMED_ACTIVE | RU-158 | RETIRED | via RU-158; DEC-53 RETIRE (7279b35) |
| CEG-COR-160 | LOW | CONFIRMED_ACTIVE | RU-159 | RETIRED | via RU-159; DEC-53 RETIRE (7279b35) |
| CEG-COR-161 | LOW | CONFIRMED_ACTIVE | RU-160 | RETIRED | via RU-160; DEC-53 RETIRE (7279b35) |
| CEG-COR-162 | LOW | CONFIRMED_ACTIVE | RU-161 | RETIRED | via RU-161; DEC-53 RETIRE (7279b35) |
| CEG-COR-163 | LOW | CONFIRMED_ACTIVE | RU-162 | RETIRED | via RU-162; DEC-53 RETIRE (7279b35) |
| CEG-COR-164 | MEDIUM | CONFIRMED_ACTIVE | RU-059 | DONE | via RU-059 |
| CEG-COR-165 | LOW | DUPLICATE | RU-055 | DUPLICATE | duplicate of CEG-COR-151 |
| CEG-COR-166 | LOW | CONFIRMED_ACTIVE | RU-031 | DONE | via RU-031 |
| CEG-COR-167 | LOW | CONFIRMED_ACTIVE | RU-032 | DONE | via RU-032 |
| CEG-COR-168 | LOW | CONFIRMED_ACTIVE | RU-098 | DONE | via RU-098 |
| CEG-COR-169 | LOW | CONFIRMED_ACTIVE | RU-060 | DONE | via RU-060 |
| CEG-COR-170 | LOW | CONFIRMED_ACTIVE | RU-095 | DONE | via RU-095 |
| CEG-COR-171 | LOW | CONFIRMED_ACTIVE | RU-016 | DONE | via RU-016 |
| CEG-COR-172 | LOW | INVALID | — | INVALID | Not a defect. The consumer's own contract pins the implemented semantics. |
| CEG-COR-173 | LOW | CONFIRMED_ACTIVE | RU-092 | DONE | via RU-092 |
| CEG-COR-174 | LOW | INVALID | — | INVALID | No observable defect: the summing consumer is unreachable and no contract defines aggregation. |
| CEG-COR-175 | LOW | INVALID | — | INVALID | Mechanism reproduces but matches the module's stated design; module is unreachable (CrossDimensionalEnsemble has no production caller; engine/scoring/confidence |
| CEG-COR-176 | LOW | CONFIRMED_ACTIVE | RU-129 | DONE | via RU-129 |
| CEG-COR-177 | LOW | CONFIRMED_ACTIVE | RU-103 | DONE | via RU-103 |
| CEG-COR-178 | LOW | INVALID | — | INVALID | No violated obligation. |
| CEG-COR-179 | LOW | BLOCKED | — | MOOT_RETIRED | Plan disposition BLOCKED (evidence outside repo: core.schemas.SemanticSearchRequest, L9 memory substrate). The code it concerns (agents/cursor memory client) is deleted under DEC-53 (7279b35), so no corrective obligation remains in this repository. |
| CEG-COR-180 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-181 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-182 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-183 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-184 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no corrective obligation. |
| CEG-COR-185 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no corrective obligation. |
| CEG-COR-186 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no corrective obligation. |
| CEG-COR-187 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no corrective obligation. |
| CEG-COR-188 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no corrective obligation. |
| CEG-COR-189 | LOW | INVALID | — | INVALID | Disproof confirmed. |
| CEG-COR-190 | LOW | INVALID | — | INVALID | Disproof confirmed. |
| CEG-COR-191 | LOW | INVALID | — | INVALID | Disproof confirmed. |
| CEG-COR-192 | LOW | INVALID | — | INVALID | Disproof confirmed. |
| CEG-COR-193 | LOW | INVALID | — | INVALID | Audit disproof confirmed under default configuration. |
| CEG-COR-194 | LOW | INVALID | — | INVALID | Audit disproof confirmed. |
| CEG-COR-195 | LOW | INVALID | — | INVALID | Disproof holds. |
| CEG-COR-196 | LOW | INVALID | — | INVALID | Disproof holds. |
| CEG-COR-197 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-198 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-199 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-200 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-201 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-202 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-203 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-204 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-205 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-206 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-207 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-208 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-209 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-210 | LOW | INVALID | — | INVALID | Audit disproof confirmed; no defect. |
| CEG-COR-211 | LOW | INVALID | — | INVALID | Disproof confirmed; no defect. |
| CEG-COR-212 | LOW | INVALID | — | INVALID | Disproof confirmed. |
| CEG-COR-213 | LOW | INVALID | — | INVALID | Disproof confirmed; no defect. |
| CEG-COR-214 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-215 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-216 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-217 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-218 | LOW | INVALID | — | INVALID | Disproof confirmed: tool resolves its APIs and fails closed. |
| CEG-COR-219 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-220 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-221 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-222 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-223 | LOW | INVALID | — | INVALID | Audit DISPROVEN; disproof re-confirmed. |
| CEG-COR-224 | LOW | INVALID | — | INVALID | Audit disproof confirmed for served packs. |
| CEG-COR-225 | LOW | INVALID | — | INVALID | Disproof confirmed: no allow-list bypass. |
| CEG-COR-226 | LOW | INVALID | — | INVALID | Disproof confirmed: no privilege boundary crossed. |
| CEG-COR-227 | LOW | INVALID | — | INVALID | Disproof confirmed as an active defect: source-level hazards exist but are unreachable (module import fails) and the inputs are the operator's own argv/defaults |
| CEG-COR-228 | LOW | INVALID | — | INVALID | Disproof confirmed. |
| CEG-COR-229 | LOW | INVALID | — | INVALID | Disproof confirmed for the assigned scope. |
| CEG-COR-230 | UNKNOWN | INVALID | — | INVALID | Disproof holds for supported (non-dev) configurations. |
| CEG-COR-231 | UNKNOWN | INVALID | — | INVALID | The behavior matches the published contract. |
| CEG-COR-232 | UNKNOWN | INVALID | — | INVALID | Disproof holds for the guard and the compare. |

## External blockers and follow-ups (not closable in this repository)

- **DEC-15 option A** — re-serving mortgage-brokerage needs a gate construct that constrains a non-candidate alias (state licensing) and a query-anchored edge (borrower blacklist). The pack stays dormant until then (owner constraint).
- **DEC-56** — rotate `MCP_API_KEY_C` (sent in cleartext to 46.62.243.82 by the retired client's old default). Human/infra action.
- **DEC-08 option B** — end-to-end HGKR Pass-2 options remain fail-closed at admission; implementation is separate feature work.
- **Release ordering** — existing PostgreSQL databases must re-apply `engine/packet/packet_store.sql` in the same release as the RU-066 tenant-scoped erasure (the unscoped 2-argument function is dropped); live GDPR erasure stays disabled by default.
- **Out-of-audit observations** PO-01..PO-06 from the plan remain recorded, not planned.
