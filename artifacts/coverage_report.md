# L9 Spec Coverage Report

- Generated: 2026-07-24T21:09:12.516089+00:00
- Template tag: `L9_TEMPLATE`

## Summary

| Category | Implemented | Partial | Missing | Total |
|----------|-------------|---------|---------|-------|
| gates | 10 | 0 | 0 | 10 |
| scoring | 7 | 0 | 0 | 7 |
| v1.1_node | 2 | 0 | 0 | 2 |
| v1.1_edge | 2 | 0 | 0 | 2 |
| v1.1_action | 0 | 2 | 0 | 2 |
| v1.1_scoring | 1 | 1 | 0 | 2 |
| action_handler | 0 | 6 | 0 | 6 |
| gds_algorithm | 5 | 0 | 0 | 5 |
| research_pattern | 5 | 0 | 0 | 5 |
| **TOTAL** | **32** | **9** | **0** | **41** |

## ⚠️ PARTIAL

### v1.1_action → `outcomes`
- Spec ref: `v1.1 addition: outcomes action/endpoint`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:1587, engine/handlers.py:2013

### v1.1_action → `resolve`
- Spec ref: `v1.1 addition: resolve action/endpoint`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:1780, engine/handlers.py:2014

### v1.1_scoring → `outcome_weighted`
- Spec ref: `v1.1 addition: outcome_weighted scoring type`
- Found in: `tools/spec_extract.py`
- Lines: tools/spec_extract.py:205, tools/spec_extract.py:262

### action_handler → `match`
- Spec ref: `chassis action: match`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:385, engine/handlers.py:1431, engine/handlers.py:1478, engine/handlers.py:2010

### action_handler → `sync`
- Spec ref: `chassis action: sync`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:637, engine/handlers.py:2011

### action_handler → `admin`
- Spec ref: `chassis action: admin`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:767, engine/handlers.py:2012

### action_handler → `query`
- Spec ref: `chassis action: query`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:391, engine/handlers.py:1435, engine/handlers.py:1461, engine/handlers.py:1470

### action_handler → `enrich`
- Spec ref: `chassis action: enrich`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:1934, engine/handlers.py:2017

### action_handler → `healthcheck`
- Spec ref: `chassis action: healthcheck`
- Found in: `engine/handlers.py`
- Lines: engine/handlers.py:1929, engine/handlers.py:2016

## ✅ IMPLEMENTED

### gates → `range`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_3.py`, `agents/cursor/cursor_workflow_kernel.yaml`, `agents/cursor/gmp_meta_learning.py`, `agents/cursor/gmp_protocol/gmp-contract.yaml`, `agents/cursor/gmp_protocol/gmp-report-contract.yaml`, `agents/cursor/gmp_protocol/phase0-scope-template.yaml`, `agents/cursor/integrations/cursor_gateway.py`, `docs/PlasticOS Graph Cognitive Engine.yaml`, `docs/contracts/config/env-contract.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/freight_matching_domain_spec.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/legal_discovery_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/arbitration/schema.py`, `engine/config/schema.py`, `engine/diagnostics/fingerprint.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/gds/scheduler.py`, `engine/handlers.py`, `engine/intake/intake_compiler.py`, `engine/kge/beam_search.py`, `engine/kge/compound_e3d.py`, `engine/kge/cross_dimensional_ensemble.py`, `engine/kge/ensemble.py`, `engine/kge/pareto_ensemble.py`, `engine/kge/transformations.py`, `engine/personas/composer.py`, `engine/scoring/assembler.py`, `engine/scoring/calibration.py`, `engine/scoring/pareto.py`, `engine/traversal/multihop.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/contracts/test_env_contract.py`, `tests/integration/test_hoprag_pipeline.py`, `tests/integration/test_match_handler.py`, `tests/integration/test_null_semantics.py`, `tests/performance/test_query_latency.py`, `tests/performance/test_sync_throughput.py`, `tests/property/test_gates_property.py`, `tests/property/test_scoring_property.py`, `tests/scoring/test_benchmark.py`, `tests/test_boot_and_registry.py`, `tests/test_pareto_wiring.py`, `tests/unit/test_arbitration.py`, `tests/unit/test_causal_edges.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_counterfactual.py`, `tests/unit/test_edge_merger.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_health_engine.py`, `tests/unit/test_helpfulness.py`, `tests/unit/test_hgkr_schema.py`, `tests/unit/test_intake.py`, `tests/unit/test_kge_ensemble.py`, `tests/unit/test_multihop_traversal.py`, `tests/unit/test_persona_selector.py`, `tests/unit/test_pseudo_query.py`, `tests/unit/test_safe_eval.py`, `tests/unit/test_wave1_invariants.py`, `tests/unit/test_wave2_calibration.py`, `tests/unit/test_wave2_feedback.py`, `tests/unit/test_wave2_normalization.py`, `tests/unit/test_wave4_state_resilience.py`, `tools/audit_engine.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:136, tools/spec_extract.py:142, tools/spec_extract.py:144, tools/spec_extract.py:146, tools/audit_engine.py:76

### gates → `threshold`
- Spec ref: `gates (detected in spec text)`
- Found in: `.github/pr_review_config.yaml`, `agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_3.py`, `agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_4.py`, `agents/cursor/cursor_memory_client.py`, `agents/cursor/cursor_memory_kernel.py`, `agents/cursor/cursor_memory_kernel.yaml`, `agents/cursor/cursor_workflow_kernel.yaml`, `agents/cursor/gmp_meta_learning.py`, `chassis/auth/settings.py`, `chassis/middleware.py`, `config/perplexity_audit.yaml`, `docs/PlasticOS Graph Cognitive Engine.yaml`, `docs/contracts/config/env-contract.yaml`, `docs/contracts/data/graph-schema.yaml`, `docs/contracts/dependencies/neo4j.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/aios_god_agent_domain_spec.yaml`, `domains/executive_assistant_domain_spec.yaml`, `domains/freight_matching_domain_spec.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/repo_as_agent_domain_spec.yaml`, `domains/research_agent_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/arbitration/engine.py`, `engine/causal/causal_compiler.py`, `engine/causal/causal_validator.py`, `engine/config/schema.py`, `engine/config/settings.py`, `engine/diagnostics/dissimilarity.py`, `engine/feedback/convergence.py`, `engine/feedback/drift_detector.py`, `engine/feedback/pattern_matcher.py`, `engine/feedback/signal_weights.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/gds/scheduler.py`, `engine/graph/circuit_breaker.py`, `engine/graph/driver.py`, `engine/handlers.py`, `engine/health/nightly_health_scan.py`, `engine/health/readiness_scorer.py`, `engine/hoprag/config.py`, `engine/hoprag/indexer.py`, `engine/inference_rule_registry.py`, `engine/intake/crm_field_scanner.py`, `engine/intake/impact_reporter.py`, `engine/intake/intake_compiler.py`, `engine/kge/beam_search.py`, `engine/kge/ensemble.py`, `engine/personas/constants.py`, `engine/personas/selector.py`, `engine/resolution/resolver.py`, `engine/resolution/similarity.py`, `engine/scoring/assembler.py`, `engine/scoring/calibration.py`, `engine/scoring/confidence.py`, `engine/scoring/hgkr_utils.py`, `engine/scoring/pareto.py`, `engine/scoring/pareto_integrator.py`, `engine/traversal/edge_merger.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_env_contract.py`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/integration/test_null_semantics.py`, `tests/integration/test_outcomes_handler.py`, `tests/invariants/test_configuration.py`, `tests/property/test_gates_property.py`, `tests/scoring/test_benchmark.py`, `tests/test_algorithmic_upgrades.py`, `tests/test_boot_and_registry.py`, `tests/test_pareto_wiring.py`, `tests/unit/test_arbitration.py`, `tests/unit/test_causal_edges.py`, `tests/unit/test_causal_validator.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_domain_spec_activation.py`, `tests/unit/test_drift_detector.py`, `tests/unit/test_edge_merger.py`, `tests/unit/test_feedback_loop.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_health_engine.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_kge_beam_search.py`, `tests/unit/test_kge_ensemble.py`, `tests/unit/test_negative_penalty.py`, `tests/unit/test_pattern_matcher.py`, `tests/unit/test_persona_selector.py`, `tests/unit/test_resolver.py`, `tests/unit/test_scoring_security.py`, `tests/unit/test_scoring_weights.py`, `tests/unit/test_settings.py`, `tests/unit/test_signal_weights.py`, `tests/unit/test_wave1_invariants.py`, `tests/unit/test_wave2_confidence.py`, `tests/unit/test_wave4_state_resilience.py`, `tests/unit/test_wave6_dormant_features.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:137, chassis/middleware.py:75, chassis/middleware.py:78, chassis/middleware.py:80, chassis/middleware.py:89

### gates → `boolean`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_2.py`, `agents/cursor/cursor_workflow_kernel.yaml`, `agents/cursor/gmp_meta_learning.py`, `docs/PlasticOS Graph Cognitive Engine.yaml`, `docs/contracts/config/env-contract.yaml`, `docs/contracts/data/graph-schema.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/legal_discovery_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/research_agent_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/config/schema.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/gds/scheduler.py`, `engine/intake/intake_compiler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/contracts/test_env_contract.py`, `tests/integration/test_null_semantics.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gate_compiler.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_handlers_enrich_health.py`, `tests/unit/test_intake.py`, `tests/unit/test_safe_eval.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:138, tests/test_boot_and_registry.py:94, tests/test_boot_and_registry.py:118, tests/unit/test_handlers_enrich_health.py:44, tests/unit/test_handlers_enrich_health.py:45

### gates → `composite`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/_template.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/arbitration/engine.py`, `engine/arbitration/schema.py`, `engine/config/schema.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/health/field_health.py`, `engine/intake/intake_schema.py`, `engine/personas/__init__.py`, `engine/personas/composer.py`, `engine/personas/constants.py`, `engine/personas/selector.py`, `engine/personas/types.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/property/test_gates_property.py`, `tests/property/test_scoring_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_arbitration.py`, `tests/unit/test_persona_composer.py`, `tests/unit/test_persona_selector.py`, `tests/unit/test_scoring_assembler.py`, `tools/check_action_refs.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:139, tools/check_action_refs.py:71, tests/test_boot_and_registry.py:95, tests/unit/test_arbitration.py:36, tests/unit/test_arbitration.py:45

### gates → `enum_map`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/freight_matching_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/config/schema.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/intake/intake_compiler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_intake.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:140, tests/test_boot_and_registry.py:96, tests/unit/test_intake.py:118, tests/unit/test_gates_all_types.py:131, tests/unit/test_gates_all_types.py:132

### gates → `exclusion`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `config/perplexity_audit.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/config/schema.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/gds/scheduler.py`, `engine/personas/constants.py`, `engine/personas/synthesis.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/integration/test_multi_tenant.py`, `tests/integration/test_null_semantics.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_intake.py`, `tests/unit/test_wave1_invariants.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:141, tests/test_boot_and_registry.py:97, tests/unit/test_config_schema.py:77, tests/unit/test_config_schema.py:147, tests/unit/test_config_schema.py:209

### gates → `self_range`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/config/schema.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:142, tests/test_boot_and_registry.py:98, tests/unit/test_gates_all_types.py:161, tests/unit/test_gates_all_types.py:162, tests/unit/test_gates_all_types.py:165

### gates → `freshness`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `chassis/auth/settings.py`, `docs/contracts/api/openapi.yaml`, `docs/contracts/config/env-contract.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/research_agent_domain_spec.yaml`, `engine/boot.py`, `engine/config/schema.py`, `engine/config/settings.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/health/gap_prioritizer.py`, `engine/health/readiness_scorer.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_data_models.py`, `tests/contracts/test_env_contract.py`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/integration/test_null_semantics.py`, `tests/property/test_gates_property.py`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_settings.py`, `tests/unit/test_wave1_invariants.py`, `tests/unit/test_wave2_confidence.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:143, tests/test_boot_and_registry.py:99, engine/boot.py:47, engine/boot.py:52, chassis/auth/settings.py:78

### gates → `temporal_range`
- Spec ref: `gates (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/freight_matching_domain_spec.yaml`, `domains/legal_discovery_domain_spec.yaml`, `engine/config/schema.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/test_boot_and_registry.py`, `tests/unit/test_gates_all_types.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:144, tests/test_boot_and_registry.py:100, tests/unit/test_gates_all_types.py:195, tests/unit/test_gates_all_types.py:196, tests/unit/test_gates_all_types.py:199

### gates → `traversal`
- Spec ref: `gates (detected in spec text)`
- Found in: `.github/pr_review_config.yaml`, `agents/cursor/cursor_workflow_kernel.yaml`, `chassis/auth/app.py`, `docs/contracts/agents/tool-schemas/_index.yaml`, `docs/contracts/config/env-contract.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/aios_god_agent_domain_spec.yaml`, `domains/executive_assistant_domain_spec.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/repo_as_agent_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/compliance/engine.py`, `engine/config/__init__.py`, `engine/config/loader.py`, `engine/config/schema.py`, `engine/config/settings.py`, `engine/gates/compiler.py`, `engine/gates/null_semantics.py`, `engine/gates/registry.py`, `engine/gates/types/__init__.py`, `engine/gates/types/all_gates.py`, `engine/handlers.py`, `engine/hoprag/__init__.py`, `engine/hoprag/config.py`, `engine/hoprag/indexer.py`, `engine/scoring/assembler.py`, `engine/scoring/helpfulness.py`, `engine/scoring/importance.py`, `engine/security/_5_llm_security.py`, `engine/traversal/__init__.py`, `engine/traversal/assembler.py`, `engine/traversal/edge_merger.py`, `engine/traversal/multihop.py`, `engine/traversal/pseudo_query.py`, `engine/traversal/resolver.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/conftest.py`, `tests/contracts/test_contracts.py`, `tests/integration/test_hoprag_pipeline.py`, `tests/integration/test_match_handler.py`, `tests/integration/test_null_semantics.py`, `tests/invariants/test_trust_boundary.py`, `tests/performance/test_query_latency.py`, `tests/test_boot_and_registry.py`, `tests/test_config_loader.py`, `tests/test_handlers.py`, `tests/test_scoring_extended.py`, `tests/unit/test_causal_serializer.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_domain_pack_loader.py`, `tests/unit/test_edge_merger.py`, `tests/unit/test_gates_all_types.py`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_multihop_traversal.py`, `tests/unit/test_parameter_resolver.py`, `tests/unit/test_pseudo_query.py`, `tests/unit/test_sync_and_traversal.py`, `tests/unit/test_traversal.py`, `tests/unit/test_traversal_assembler.py`, `tests/unit/test_wave1_invariants.py`, `tools/audit_rules.yaml`, `tools/hoprag_benchmark.py`, `tools/l9_meta_injector.py`, `tools/spec_extract.py`, `tools/validate_domain.py`
- Lines: tools/hoprag_benchmark.py:12, tools/hoprag_benchmark.py:16, tools/hoprag_benchmark.py:176, tools/spec_extract.py:145, tools/validate_domain.py:8

### scoring → `geo_decay`
- Spec ref: `scoring (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `chassis/auth/settings.py`, `docs/contracts/config/env-contract.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/freight_matching_domain_spec.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/config/schema.py`, `engine/config/settings.py`, `engine/kge/cross_dimensional_ensemble.py`, `engine/scoring/assembler.py`, `engine/scoring/pareto_integrator.py`, `engine/scoring/weight_discovery.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/scoring/test_benchmark.py`, `tests/test_pareto_wiring.py`, `tests/unit/test_confidence_intervals.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_counterfactual.py`, `tests/unit/test_cross_dimensional_ensemble.py`, `tests/unit/test_negative_penalty.py`, `tests/unit/test_scoring.py`, `tests/unit/test_settings.py`, `tests/unit/test_signal_weights.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:198, tests/test_pareto_wiring.py:45, tests/test_pareto_wiring.py:52, tests/test_pareto_wiring.py:55, tests/test_pareto_wiring.py:56

### scoring → `log_normalized`
- Spec ref: `scoring (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/legal_discovery_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/research_agent_domain_spec.yaml`, `engine/config/schema.py`, `engine/scoring/assembler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/scoring/test_benchmark.py`, `tests/unit/test_intake.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:199, tests/unit/test_intake.py:126, tests/contracts/test_contracts.py:411, tests/scoring/test_benchmark.py:105, engine/config/schema.py:95

### scoring → `community_match`
- Spec ref: `scoring (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/config/schema.py`, `engine/handlers.py`, `engine/kge/cross_dimensional_ensemble.py`, `engine/scoring/assembler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/fixtures/benchmark_data.py`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/scoring/test_benchmark.py`, `tests/unit/test_counterfactual.py`, `tests/unit/test_cross_dimensional_ensemble.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_hgkr_schema.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:200, engine/handlers.py:437, tests/unit/test_counterfactual.py:62, tests/unit/test_counterfactual.py:63, tests/unit/test_counterfactual.py:73

### scoring → `inverse_linear`
- Spec ref: `scoring (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/aios_god_agent_domain_spec.yaml`, `domains/executive_assistant_domain_spec.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/repo_as_agent_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/config/schema.py`, `engine/scoring/assembler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/unit/test_hgkr_schema.py`, `tests/unit/test_scoring.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:201, tests/unit/test_scoring.py:74, tests/unit/test_scoring.py:81, tests/unit/test_hgkr_schema.py:203, tests/contracts/test_contracts.py:413

### scoring → `candidate_property`
- Spec ref: `scoring (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/aios_god_agent_domain_spec.yaml`, `domains/executive_assistant_domain_spec.yaml`, `domains/freight_matching_domain_spec.yaml`, `domains/healthcare_referral_domain_spec.yaml`, `domains/legal_discovery_domain_spec.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `domains/repo_as_agent_domain_spec.yaml`, `domains/research_agent_domain_spec.yaml`, `domains/roofing_company_domain_spec.yaml`, `engine/config/schema.py`, `engine/scoring/assembler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/integration/test_null_semantics.py`, `tests/scoring/test_benchmark.py`, `tests/test_scoring_extended.py`, `tests/unit/test_confidence_intervals.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_hgkr_schema.py`, `tests/unit/test_intake.py`, `tests/unit/test_negative_penalty.py`, `tests/unit/test_scoring.py`, `tests/unit/test_scoring_weights.py`, `tests/unit/test_signal_weights.py`, `tests/unit/test_wave1_invariants.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:202, tests/test_scoring_extended.py:12, tests/test_scoring_extended.py:81, tests/test_scoring_extended.py:82, tests/test_scoring_extended.py:83

### scoring → `custom_cypher`
- Spec ref: `scoring (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/repo_as_agent_domain_spec.yaml`, `engine/config/schema.py`, `engine/scoring/assembler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/unit/test_scoring_security.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:203, tests/unit/test_scoring_security.py:12, tests/unit/test_scoring_security.py:14, tests/unit/test_scoring_security.py:25, tests/contracts/test_contracts.py:418

### scoring → `temporal_decay`
- Spec ref: `scoring (detected in spec text)`
- Found in: `domains/plasticos_domain_spec.yaml`, `engine/config/schema.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/unit/test_settings.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:204, tools/spec_extract.py:262, tests/unit/test_settings.py:57, engine/config/schema.py:861, graph-cognitive-engine-spec-v1.1.0.yaml:1968

### v1.1_node → `TransactionOutcome`
- Spec ref: `v1.1 addition: TransactionOutcome node`
- Found in: `docs/contracts/agents/tool-schemas/_index.yaml`, `docs/contracts/data/graph-schema.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/causal/attribution.py`, `engine/causal/counterfactual.py`, `engine/config/schema.py`, `engine/feedback/convergence.py`, `engine/feedback/pattern_matcher.py`, `engine/feedback/signal_weights.py`, `engine/handlers.py`, `engine/packet/packet_store.py`, `engine/resolution/similarity.py`, `engine/scoring/assembler.py`, `engine/scoring/hgkr_utils.py`, `tests/contracts/test_data_models.py`, `tests/unit/test_causal_edges.py`, `tests/unit/test_causal_serializer.py`, `tests/unit/test_domain_spec_activation.py`, `tests/unit/test_hgkr_pass2.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:259, engine/handlers.py:815, engine/handlers.py:1206, engine/handlers.py:1347, engine/handlers.py:1588

### v1.1_node → `SignalEvent`
- Spec ref: `v1.1 addition: SignalEvent node`
- Found in: `docs/plasticos_domain_spec_v0.4.yaml`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:259, docs/plasticos_domain_spec_v0.4.yaml:555, docs/plasticos_domain_spec_v0.4.yaml:1048, docs/plasticos_domain_spec_v0.4.yaml:1643

### v1.1_edge → `RESULTED_IN`
- Spec ref: `v1.1 addition: RESULTED_IN edge`
- Found in: `docs/contracts/agents/tool-schemas/_index.yaml`, `docs/contracts/data/graph-schema.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/causal/edge_taxonomy.py`, `engine/config/schema.py`, `engine/handlers.py`, `engine/resolution/similarity.py`, `engine/scoring/assembler.py`, `tests/contracts/test_data_models.py`, `tests/unit/test_causal_edges.py`, `tests/unit/test_causal_serializer.py`, `tests/unit/test_causal_validator.py`, `tests/unit/test_domain_spec_activation.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:260, engine/handlers.py:1588, engine/handlers.py:1607, engine/handlers.py:1617, engine/handlers.py:1620

### v1.1_edge → `RESOLVED_FROM`
- Spec ref: `v1.1 addition: RESOLVED_FROM edge`
- Found in: `docs/contracts/agents/tool-schemas/_index.yaml`, `docs/contracts/data/graph-schema.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `engine/handlers.py`, `engine/resolution/resolver.py`, `tests/contracts/test_data_models.py`, `tests/unit/test_handlers_extended.py`, `tests/unit/test_resolver.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:260, engine/handlers.py:1781, engine/handlers.py:1785, engine/handlers.py:1850, tests/unit/test_handlers_extended.py:307

### v1.1_scoring → `temporal_decay`
- Spec ref: `v1.1 addition: temporal_decay scoring type`
- Found in: `domains/plasticos_domain_spec.yaml`, `engine/config/schema.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/unit/test_settings.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:204, tools/spec_extract.py:262, tests/unit/test_settings.py:57, engine/config/schema.py:861, graph-cognitive-engine-spec-v1.1.0.yaml:1968

### gds_algorithm → `louvain`
- Spec ref: `gds (detected in spec text)`
- Found in: `agents/cursor/cursor_workflow_kernel.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/gds/scheduler.py`, `engine/graph/community_export.py`, `engine/scoring/assembler.py`, `engine/scoring/hgkr_utils.py`, `engine/startup_wiring.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/contracts/test_contracts.py`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/gap_fixes/test_gap2_return_channel.py`, `tests/unit/test_config_schema.py`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_handlers_enrich_health.py`, `tests/unit/test_handlers_extended.py`, `tests/unit/test_hgkr_gds_dag.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_hgkr_schema.py`, `tests/unit/test_scoring_security.py`, `tests/unit/test_wave6_dormant_features.py`, `tools/audit_rules.yaml`, `tools/l9_meta_injector.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:330, tools/l9_meta_injector.py:370, engine/startup_wiring.py:64, tests/unit/test_handlers_enrich_health.py:125, tests/unit/test_config_schema.py:341

### gds_algorithm → `cooccurrence`
- Spec ref: `gds (detected in spec text)`
- Found in: `docs/plasticos_domain_spec_v0.3.0.yaml`, `engine/gds/scheduler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_hgkr_gds_dag.py`, `tools/audit_rules.yaml`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:331, tests/unit/test_hgkr_gds_dag.py:66, tests/unit/test_hgkr_gds_dag.py:69, tests/unit/test_hgkr_gds_dag.py:70, tests/unit/test_hgkr_gds_dag.py:75

### gds_algorithm → `reinforcement`
- Spec ref: `gds (detected in spec text)`
- Found in: `chassis/auth/settings.py`, `docs/contracts/api/openapi.yaml`, `docs/contracts/config/env-contract.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/_template.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/boot.py`, `engine/config/settings.py`, `engine/gds/scheduler.py`, `engine/outcomes/engine.py`, `engine/outcomes/schema.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/conftest.py`, `tests/contracts/test_data_models.py`, `tests/contracts/test_env_contract.py`, `tests/integration/test_match_handler.py`, `tests/performance/test_query_latency.py`, `tests/unit/test_gds_scheduler.py`, `tests/unit/test_hgkr_gds_dag.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_settings.py`, `tests/unit/test_wave1_invariants.py`, `tests/unit/test_wave2_confidence.py`, `tools/audit_rules.yaml`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:332, tests/conftest.py:253, tests/conftest.py:261, tests/conftest.py:269, engine/boot.py:47

### gds_algorithm → `temporal_recency`
- Spec ref: `gds (detected in spec text)`
- Found in: `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `engine/gds/scheduler.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/unit/test_gds_scheduler.py`, `tools/audit_rules.yaml`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:333, tests/unit/test_gds_scheduler.py:215, engine/gds/scheduler.py:264, engine/gds/scheduler.py:499, graph-cognitive-engine-spec-v1.1.0.yaml:585

### gds_algorithm → `similarity`
- Spec ref: `gds (detected in spec text)`
- Found in: `agents/cursor/cursor_memory_client.py`, `agents/cursor/cursor_workflow_kernel.yaml`, `agents/cursor/integrations/cursor_gateway.py`, `docs/contracts/data/graph-schema.yaml`, `docs/plasticos_domain_spec_v0.3.0.yaml`, `docs/plasticos_domain_spec_v0.4.yaml`, `domains/MASTER-SPEC-ALL-DOMAINS.yaml`, `domains/_template.yaml`, `domains/mortgage_brokerage_domain_spec.yaml`, `domains/plasticos/spec.yaml`, `domains/plasticos_domain_spec.yaml`, `engine/config/schema.py`, `engine/diagnostics/__init__.py`, `engine/diagnostics/dissimilarity.py`, `engine/diagnostics/fingerprint.py`, `engine/feedback/pattern_matcher.py`, `engine/handlers.py`, `engine/hoprag/__init__.py`, `engine/hoprag/config.py`, `engine/hoprag/indexer.py`, `engine/intake/crm_field_scanner.py`, `engine/kge/beam_search.py`, `engine/kge/compound_e3d.py`, `engine/resolution/__init__.py`, `engine/resolution/resolver.py`, `engine/resolution/similarity.py`, `engine/scoring/assembler.py`, `engine/scoring/helpfulness.py`, `engine/scoring/hgkr_utils.py`, `engine/spec.yaml`, `engine/traversal/edge_merger.py`, `engine/traversal/multihop.py`, `engine/traversal/pseudo_query.py`, `graph-cognitive-engine-spec-v1.1.0.yaml`, `tests/fixtures/hgkr_benchmark_domain.yaml`, `tests/integration/test_hoprag_pipeline.py`, `tests/test_algorithmic_upgrades.py`, `tests/unit/test_domain_spec_activation.py`, `tests/unit/test_edge_merger.py`, `tests/unit/test_feedback_loop.py`, `tests/unit/test_helpfulness.py`, `tests/unit/test_hgkr_pass2.py`, `tests/unit/test_kge_beam_search.py`, `tests/unit/test_kge_compound_e3d.py`, `tests/unit/test_multihop_traversal.py`, `tests/unit/test_pattern_matcher.py`, `tests/unit/test_resolver.py`, `tools/spec_extract.py`
- Lines: tools/spec_extract.py:334, tests/test_algorithmic_upgrades.py:4, tests/test_algorithmic_upgrades.py:26, tests/test_algorithmic_upgrades.py:27, tests/test_algorithmic_upgrades.py:126

### research_pattern → `Behavioral Graph Collaborative Filtering`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#1_COLLABORATIVE_FILTERING_AT_SCALE`
- Found in: `engine/gds/scheduler.py`, `engine/scoring/assembler.py`
- Lines: engine/gds/scheduler.py:42, engine/gds/scheduler.py:45, engine/gds/scheduler.py:61, engine/gds/scheduler.py:259, engine/gds/scheduler.py:260

### research_pattern → `Context-Aware Entity Resolution`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#2_ENTITY_DISAMBIGUATION_VIA_CONTEXT`
- Found in: `engine/handlers.py`, `engine/resolution/resolver.py`
- Lines: engine/resolution/resolver.py:30, engine/handlers.py:1796, engine/handlers.py:1798

### research_pattern → `Low-Dimensional Similarity via Graph Embeddings`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#3_GRAPH_EMBEDDINGS_FOR_SIMILARITY`
- Found in: `engine/config/schema.py`, `engine/config/settings.py`, `engine/kge/__init__.py`, `engine/kge/beam_search.py`, `engine/kge/compound_e3d.py`, `engine/kge/ensemble.py`, `engine/kge/transformations.py`
- Lines: engine/kge/transformations.py:12, engine/kge/transformations.py:199, engine/kge/compound_e3d.py:12, engine/kge/compound_e3d.py:15, engine/kge/compound_e3d.py:43

### research_pattern → `Session-Context Dynamic Re-Ranking`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#4_REAL_TIME_CONTEXT_AWARE_RANKING`
- Found in: `engine/scoring/assembler.py`, `engine/scoring/calibration.py`, `engine/scoring/feedback.py`, `engine/scoring/hgkr_utils.py`
- Lines: engine/scoring/feedback.py:19, engine/scoring/hgkr_utils.py:7, engine/scoring/hgkr_utils.py:19, engine/scoring/hgkr_utils.py:35, engine/scoring/hgkr_utils.py:45

### research_pattern → `Transitive Relationship Discovery via Multi-Hop Traversal`
- Spec ref: `tools/research/top5_leverage_patterns_detailed.json#5_MULTI_HOP_TRAVERSAL_FOR_ENRICHMENT`
- Found in: `engine/hoprag/__init__.py`, `engine/traversal/multihop.py`
- Lines: engine/traversal/multihop.py:138, engine/traversal/multihop.py:151, engine/traversal/multihop.py:174, engine/traversal/multihop.py:283, engine/hoprag/__init__.py:30
