# L9 Spec Coverage Report

- Generated: 2026-11-14T18:24:39.033067+00:00
- Template tag: `L9_TEMPLATE`
- Status: IMPLEMENTED = implementation present in its owning code AND referenced by a test under tests/; PARTIAL = implementation present, no test references it; MISSING = no implementation present. Evidence rules per category: tools/spec_extract.py module docstring.

## Summary

| Category | Implemented | Partial | Missing | Total |
|----------|-------------|---------|---------|-------|
| gates | 10 | 0 | 0 | 10 |
| scoring | 9 | 0 | 3 | 12 |
| gds_algorithm | 6 | 0 | 2 | 8 |
| action_handler | 9 | 0 | 0 | 9 |
| research_pattern | 5 | 0 | 0 | 5 |
| **TOTAL** | **39** | **0** | **5** | **44** |

## ❌ MISSING

### scoring → `realtime_reranking`
- Spec ref: `scoringcomputationtypes[realtime_reranking]`
- **No implementation found**

### scoring → `multihop_traversal`
- Spec ref: `scoringcomputationtypes[multihop_traversal]`
- **No implementation found**

### scoring → `churn_detection`
- Spec ref: `scoringcomputationtypes[churn_detection]`
- **No implementation found**

### gds_algorithm → `custom_aggregation`
- Spec ref: `gdsjobs.patch001_nightly_jobs[reinforcement_polymer_history].algorithm; gdsjobs.patch001_weekly_jobs[competitive_overlap].algorithm; gdsjobs.patch001_weekly_jobs[market_supply_aggregation].algorithm; gdsjobs.patch001_weekly_jobs[churn_detection].algorithm`
- **No implementation found**

### gds_algorithm → `louvain_to_topology`
- Spec ref: `gdsjobs.patch001_weekly_jobs[community_membership].algorithm`
- **No implementation found**

## ✅ IMPLEMENTED

### gates → `range`
- Spec ref: `gatetypes[range]`
- Implementation: engine/config/schema.py:85, engine/gates/registry.py:39
- Tests: `tests/contracts/test_contracts.py`, `tests/integration/test_null_semantics.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_config_schema.py`

### gates → `threshold`
- Spec ref: `gatetypes[threshold]`
- Implementation: engine/config/schema.py:86, engine/gates/registry.py:40
- Tests: `tests/integration/test_null_semantics.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/test_handlers.py`, `tests/unit/test_config_schema.py`

### gates → `boolean`
- Spec ref: `gatetypes[boolean]`
- Implementation: engine/config/schema.py:87, engine/gates/registry.py:41
- Tests: `tests/contracts/test_contracts.py`, `tests/integration/test_null_semantics.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gate_compiler.py`

### gates → `composite`
- Spec ref: `gatetypes[composite]`
- Implementation: engine/config/schema.py:88, engine/gates/registry.py:42
- Tests: `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`

### gates → `enummap`
- Spec ref: `gatetypes[enummap]`
- Implementation: engine/config/schema.py:89, engine/gates/registry.py:43
- Tests: `tests/contracts/test_contracts.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_intake.py`

### gates → `exclusion`
- Spec ref: `gatetypes[exclusion]`
- Implementation: engine/config/schema.py:90, engine/gates/registry.py:44
- Tests: `tests/integration/test_null_semantics.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_cypher_lint.py`, `tests/unit/test_gates_all_types.py`

### gates → `selfrange`
- Spec ref: `gatetypes[selfrange]`
- Implementation: engine/config/schema.py:91, engine/gates/registry.py:45
- Tests: `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`

### gates → `freshness`
- Spec ref: `gatetypes[freshness]`
- Implementation: engine/config/schema.py:92, engine/gates/registry.py:46
- Tests: `tests/integration/test_null_semantics.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`

### gates → `temporalrange`
- Spec ref: `gatetypes[temporalrange]`
- Implementation: engine/config/schema.py:93, engine/gates/registry.py:47
- Tests: `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_health_engine.py`, `tests/unit/test_intake.py`

### gates → `traversal`
- Spec ref: `gatetypes[traversal]`
- Implementation: engine/config/schema.py:94, engine/gates/registry.py:48
- Tests: `tests/integration/test_null_semantics.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_gates_all_types.py`

### scoring → `geodecay`
- Spec ref: `scoringcomputationtypes[geodecay]`
- Implementation: engine/config/schema.py:107, engine/scoring/assembler.py:320
- Tests: `tests/contracts/test_contracts.py`, `tests/scoring/test_benchmark.py`, `tests/unit/test_confidence_intervals.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_counterfactual.py`

### scoring → `lognormalized`
- Spec ref: `scoringcomputationtypes[lognormalized]`
- Implementation: engine/config/schema.py:108, engine/scoring/assembler.py:321
- Tests: `tests/contracts/test_contracts.py`, `tests/integration/test_clamp_nan.py`, `tests/scoring/test_benchmark.py`, `tests/unit/test_intake.py`, `tests/unit/test_scoring_dimension_metadata.py`

### scoring → `communitymatch`
- Spec ref: `scoringcomputationtypes[communitymatch]`
- Implementation: engine/config/schema.py:109, engine/scoring/assembler.py:322
- Tests: `tests/contracts/test_contracts.py`, `tests/scoring/test_benchmark.py`, `tests/unit/test_counterfactual.py`, `tests/unit/test_domain_neutrality_acceptance.py`, `tests/unit/test_domain_pack_shape.py`

### scoring → `inverselinear`
- Spec ref: `scoringcomputationtypes[inverselinear]`
- Implementation: engine/config/schema.py:110, engine/scoring/assembler.py:323
- Tests: `tests/contracts/test_contracts.py`, `tests/unit/test_hgkr_schema.py`, `tests/unit/test_scoring.py`

### scoring → `candidateproperty`
- Spec ref: `scoringcomputationtypes[candidateproperty]`
- Implementation: engine/config/schema.py:111, engine/scoring/assembler.py:335
- Tests: `tests/compliance/test_prohibited_factors.py`, `tests/contracts/test_contracts.py`, `tests/scoring/test_benchmark.py`, `tests/test_compliance_engine.py`, `tests/test_scoring_extended.py`

### scoring → `weightedrate`
- Spec ref: `scoringcomputationtypes[weightedrate]`
- Implementation: engine/config/schema.py:112, engine/scoring/assembler.py:324
- Tests: `tests/contracts/test_contracts.py`

### scoring → `pricealignment`
- Spec ref: `scoringcomputationtypes[pricealignment]`
- Implementation: engine/config/schema.py:113, engine/scoring/assembler.py:325
- Tests: `tests/contracts/test_contracts.py`, `tests/integration/test_pricealignment_integer_operands.py`, `tests/unit/test_counterfactual.py`, `tests/unit/test_domain_pack_shape.py`, `tests/unit/test_scoring_assembler.py`

### scoring → `temporalproximity`
- Spec ref: `scoringcomputationtypes[temporalproximity]`
- Implementation: engine/config/schema.py:114, engine/scoring/assembler.py:326
- Tests: `tests/contracts/test_contracts.py`

### scoring → `customcypher`
- Spec ref: `scoringcomputationtypes[customcypher]`
- Implementation: engine/config/schema.py:115, engine/scoring/assembler.py:337
- Tests: `tests/contracts/test_contracts.py`

### gds_algorithm → `louvain`
- Spec ref: `gdsjobs.algorithms[louvain]`
- Implementation: engine/gds/scheduler.py:396
- Tests: `tests/contracts/test_contracts.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_domain_neutrality_acceptance.py`, `tests/unit/test_gds_node_similarity.py`, `tests/unit/test_gds_scheduler.py`

### gds_algorithm → `cooccurrence`
- Spec ref: `gdsjobs.algorithms[cooccurrence]`
- Implementation: engine/gds/scheduler.py:402
- Tests: `tests/integration/test_gds_cooccurrence_prune.py`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_hgkr_gds_dag.py`

### gds_algorithm → `reinforcement`
- Spec ref: `gdsjobs.algorithms[reinforcement]`
- Implementation: engine/gds/scheduler.py:404
- Tests: `tests/contracts/test_data_models.py`, `tests/unit/test_domain_neutrality_acceptance.py`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_hgkr_gds_dag.py`, `tests/unit/test_hgkr_pass2.py`

### gds_algorithm → `temporal_recency`
- Spec ref: `gdsjobs.algorithms[temporal_recency]`
- Implementation: engine/gds/scheduler.py:406
- Tests: `tests/integration/test_gds_temporal_recency.py`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_hgkr_gds_dag.py`

### gds_algorithm → `equipment_sync`
- Spec ref: `gdsjobs.algorithms[equipment_sync]`
- Implementation: engine/gds/scheduler.py:410
- Tests: `tests/unit/test_gds_scheduler.py`, `tests/unit/test_hgkr_gds_dag.py`

### gds_algorithm → `nodeSimilarity`
- Spec ref: `gdsjobs.patch001_weekly_jobs[form_cooccurrence].algorithm; gdsjobs.patch001_weekly_jobs[capability_similarity].algorithm`
- Implementation: engine/gds/scheduler.py:416
- Tests: `tests/integration/test_gds_node_similarity.py`, `tests/unit/test_gds_node_similarity.py`

### action_handler → `match`
- Spec ref: `endpoints.universal[/v1/match]`
- Implementation: engine/handlers.py:3601
- Tests: `tests/integration/test_handlers.py`, `tests/integration/test_match_gate_row_scope.py`, `tests/integration/test_match_handler.py`, `tests/integration/test_sync_handler.py`, `tests/test_handlers.py`

### action_handler → `sync`
- Spec ref: `endpoints.universal[/v1/sync/{entity_type}]`
- Implementation: engine/handlers.py:3602
- Tests: `tests/integration/test_graph_evidence_score.py`, `tests/integration/test_handlers.py`, `tests/integration/test_sync_handler.py`, `tests/integration/test_tenant_mapping_identity.py`, `tests/integration/test_typed_sync_contract.py`

### action_handler → `outcomes`
- Spec ref: `endpoints.universal[/v1/outcomes]`
- Implementation: engine/handlers.py:3604
- Tests: `tests/contracts/test_data_models.py`, `tests/integration/test_feedback_convergence_closure.py`, `tests/integration/test_outcomes_causal_integrity.py`, `tests/integration/test_outcomes_handler.py`, `tests/test_handlers.py`

### action_handler → `resolve`
- Spec ref: `endpoints.universal[/v1/resolve/{entity_type}]`
- Implementation: engine/handlers.py:3605
- Tests: `tests/contracts/test_data_models.py`, `tests/test_handlers.py`, `tests/unit/test_declared_node_identity.py`, `tests/unit/test_handlers_extended.py`, `tests/unit/test_rbac_required_proofs.py`

### action_handler → `admin`
- Spec ref: `endpoints.universal[/v1/admin/rebuild]`
- Implementation: engine/handlers.py:3603
- Tests: `tests/architecture/test_seam_gate_only.py`, `tests/contracts/test_contracts.py`, `tests/contracts/test_tool_schemas.py`, `tests/integration/test_admin_handler.py`, `tests/integration/test_handlers.py`

### action_handler → `domains`
- Spec ref: `endpoints.universal[/v1/domains] — realized as admin subaction list_domains: the engine lists domain packs through admin subaction list_domains; the chassis owns the HTTP route`
- Implementation: engine/handlers.py:3603, engine/handlers.py:1449
- Tests: `tests/architecture/test_seam_gate_only.py`, `tests/integration/test_admin_handler.py`, `tests/integration/test_handlers.py`, `tests/test_handlers.py`, `tests/unit/test_handlers_extended.py`

### action_handler → `health`
- Spec ref: `endpoints.universal[/v1/health]`
- Implementation: engine/handlers.py:3606
- Tests: `tests/test_handlers.py`

### action_handler → `healthcheck`
- Spec ref: `engine contract: AGENTS.md Project Structure (engine/handlers.py action handlers) — chassis liveness action the engine declares; the spec's endpoints section predates it`
- Implementation: engine/handlers.py:3607
- Tests: `tests/test_enrich.py`, `tests/unit/test_handlers_enrich_health.py`

### action_handler → `enrich`
- Spec ref: `engine contract: AGENTS.md Project Structure (engine/handlers.py action handlers) — enrichment action the engine declares; the spec's endpoints section predates it`
- Implementation: engine/handlers.py:3609
- Tests: `tests/test_enrich.py`, `tests/unit/test_declared_node_identity.py`, `tests/unit/test_handlers_enrich_health.py`

### research_pattern → `Behavioral Graph Collaborative Filtering`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#1_COLLABORATIVE_FILTERING_AT_SCALE`
- Implementation: engine/gds/scheduler.py:55, engine/gds/scheduler.py:58, engine/gds/scheduler.py:74, engine/gds/scheduler.py:402, engine/gds/scheduler.py:403
- Tests: `tests/contracts/test_contracts.py`, `tests/integration/test_gds_cooccurrence_prune.py`, `tests/scoring/test_benchmark.py`, `tests/unit/test_counterfactual.py`, `tests/unit/test_domain_neutrality_acceptance.py`

### research_pattern → `Context-Aware Entity Resolution`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#2_ENTITY_DISAMBIGUATION_VIA_CONTEXT`
- Implementation: engine/handlers.py:2981, engine/handlers.py:2983, engine/resolution/resolver.py:59
- Tests: `tests/contracts/test_data_models.py`, `tests/integration/test_resolver_merge_order.py`, `tests/test_handlers.py`, `tests/unit/test_declared_node_identity.py`, `tests/unit/test_handlers_extended.py`

### research_pattern → `Low-Dimensional Similarity via Graph Embeddings`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#3_GRAPH_EMBEDDINGS_FOR_SIMILARITY`
- Implementation: engine/config/schema.py:1002, engine/config/settings.py:116, engine/kge/__init__.py:14, engine/kge/__init__.py:21, engine/kge/__init__.py:43
- Tests: `tests/contracts/test_contracts.py`, `tests/invariants/test_compliance.py`, `tests/unit/test_admin_tenant_binding.py`, `tests/unit/test_api_error_handling.py`, `tests/unit/test_config_schema.py`

### research_pattern → `Session-Context Dynamic Re-Ranking`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#4_REAL_TIME_CONTEXT_AWARE_RANKING`
- Implementation: engine/scoring/assembler.py:326, engine/scoring/assembler.py:493, engine/scoring/calibration.py:7, engine/scoring/calibration.py:12, engine/scoring/calibration.py:15
- Tests: `tests/contracts/test_contracts.py`, `tests/unit/test_admin_tenant_binding.py`, `tests/unit/test_domain_admission_rules.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_outcome_vocabulary_required.py`

### research_pattern → `Transitive Relationship Discovery via Multi-Hop Traversal`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#5_MULTI_HOP_TRAVERSAL_FOR_ENRICHMENT`
- Implementation: engine/hoprag/__init__.py:30, engine/traversal/multihop.py:136, engine/traversal/multihop.py:149, engine/traversal/multihop.py:173, engine/traversal/multihop.py:282
- Tests: `tests/integration/test_hoprag_pipeline.py`, `tests/unit/test_multihop_traversal.py`
