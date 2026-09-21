---
paths:
  - "engine/**/*.py"
---
# Capability Registry — Do NOT Duplicate

Before building any of these, check if it already exists. Use the existing implementation.

| Capability | File | Key Class/Function |
|-----------|------|-------------------|
| Score clamping [0, 1] | engine/scoring/assembler.py | `_clamp_expression()` |
| Weight-sum validation | engine/handlers.py | `handle_match()` weight check |
| Startup weight assertion | engine/boot.py | `_assert_default_weight_sum()` |
| Domain cross-ref validation | engine/config/schema.py | Pydantic model validators |
| Gate null-param checking | engine/gates/compiler.py | `validate_gates()` |
| Traversal bounds | engine/traversal/assembler.py | `validate_traversal()` |
| Param strict mode | engine/traversal/resolver.py | `ValidationError` on failure |
| Score calibration | engine/scoring/calibration.py | `ScoreCalibration` |
| Confidence bounds | engine/scoring/confidence.py | `ConfidenceChecker` |
| Weight feedback loop | engine/scoring/feedback.py | outcome recording + gradient |
| Score normalization | engine/handlers.py | post-query min-max pass |
| Entity resolution | engine/resolution/resolver.py | `EntityResolver` |
| Causal edges | engine/causal/causal_compiler.py | `CausalCompiler` |
| Causal attribution temporal decay | engine/causal/attribution.py | `_apply_temporal_decay()` / `_temporal_decay_factor()` |
| Persona composition | engine/personas/composer.py | trait vector arithmetic |
| Health scoring | engine/health/ | readiness, gaps, enrichment |
| CRM intake | engine/intake/ | scan, compile, impact |
| Pareto scoring | engine/scoring/pareto.py | multi-objective optimization |
| Convergence loop | engine/feedback/convergence.py | `ConvergenceLoop` |
| Label sanitization | engine/utils/security.py | `sanitize_label()` |
| CEG → Gate → EIE egress (the ONLY peer egress) | engine/gate_egress.py | `request_enrichment()`, `emit_graph_inference_result()` |
| Inference outputs → EIE wire shape | engine/gate_egress.py | `build_inference_outputs()` (0.55 floor, matches EIE) |
| Domain database provisioning | engine/graph/driver.py | `GraphDriver.ensure_database()` (flag: `auto_create_domain_database`) |
| Database-name validation for DDL | engine/utils/security.py | `sanitize_database_name()` — `sanitize_label()` forbids the dashes domain ids use |
| Gate run-time values as `$params` | engine/gates/types/all_gates.py | `BaseGate._bind_param()` / `BaseGate.query_params` |
| Cypher injection scanner (C-009) | tools/cypher_lint.py | `scan_tree()` — AST f-string role classifier |
| Inference rule execution | engine/inference_rule_registry.py | `execute_rule()`, `list_registered_rules()` |

Do not add a second outbound client. `engine/gate_egress.py` is the only
CEG → peer egress; Gate resolves the destination from the action.
