---
paths:
  - "engine/config/settings.py"
  - "engine/handlers.py"
  - "engine/boot.py"
---
# Feature Flags (engine/config/settings.py)

All in Settings class, controllable via env vars. Contract 21: every behavioral change must be flag-gated.

## Core Engine
| Flag | Default | Purpose |
|------|---------|---------|
| gds_enabled | True | GDS scheduler toggle |
| kge_enabled | False | KGE embedding scoring dimension |
| pareto_enabled | True | Multi-objective Pareto-optimal scoring |
| pareto_weight_discovery_enabled | False | Learned weight trade-offs from outcomes |

## Wave 1 — Invariant Hardening
| Flag | Default | Purpose |
|------|---------|---------|
| domain_strict_validation | True | Cross-reference validation at domain load |
| score_clamp_enabled | True | Clamp dimension scores to [0, 1] |
| strict_null_gates | True | Reject gates with null-resolved parameters |
| max_hop_hard_cap | 10 | Maximum traversal hops |
| param_strict_mode | True | Raise on derived parameter resolution failures |

## Wave 2 — Scoring Refinement
| Flag | Default | Purpose |
|------|---------|---------|
| feedback_enabled | False | Outcome feedback loop |
| confidence_check_enabled | True | Ensemble confidence bounds |
| monoculture_threshold | 0.70 | Single-dimension dominance cap |
| ensemble_max_divergence | 0.30 | GDS/KGE score divergence cap |
| score_normalize | False | Post-query min-max normalization |

## Wave 7 — Explicit Tenant Database Binding
| Flag | Default | Purpose |
|------|---------|---------|
| strict_tenant_database | False | Require explicit `database=` on GraphDriver calls; no implicit 'neo4j' fallback (W7-01) |
| require_sdk_chassis_in_prod | False | Fail startup if `L9_CHASSIS != sdk` when `l9_env == prod` (W7-02) |

## Constellation Seam (EIE ↔ Gate ↔ CEG)
| Flag | Default | Purpose |
|------|---------|---------|
| auto_enrich_via_gate | False | Dispatch `enrich` to EIE through Gate (spends EIE budget) |
| graph_inference_feedback_enabled | False | Emit `graph-inference-result` to EIE through Gate (EIE-008 / CEG-006) |
| auto_create_domain_database | False | Provision the tenant domain database on first use (CEG-008; Enterprise-only CREATE DATABASE) |
| health_api_enabled | False | Expose engine/health/api.py via admin health_* subactions (CEG-006) |

## Domain Pack Admission (generic; the engine names no domain)
| Flag | Default | Purpose |
|------|---------|---------|
| unvalidated_domain_packs_enabled | False | Serve packs whose gates do not compile to executable Cypher (`engine.config.loader.admission_defects`, CEG-009) |
| dormant_domain_packs_enabled | False | Serve packs that declare `domain.activation: dormant` (replaced the per-domain `idea_portfolio_enabled`) |

Entity resolution is tuned per domain in `DomainSpec.semantic_registry`; there are no
engine-wide resolution tolerances (the former melt-flow-index / density tolerances
were industry-specific and had no caller).

## Scoring Weights (Contract 22: sum ≤ 1.0)
| Weight | Default | Enforced by |
|--------|---------|-------------|
| w_structural | 0.30 | boot.py _assert_default_weight_sum() |
| w_geo | 0.25 | boot.py _assert_default_weight_sum() |
| w_reinforcement | 0.20 | boot.py _assert_default_weight_sum() |
| w_freshness | 0.10 | boot.py _assert_default_weight_sum() |
| **Sum** | **0.85** | Must be ≤ 1.0 |

## Stage 2 — Domain-neutral Cypher (CEG Stage 4 proof)
| Flag | Default | Purpose |
|------|---------|---------|
| traversal_gate_exists_subquery | True | Traversal gate compiles to `EXISTS { MATCH … WHERE t.prop = $param }` with query-param-only null handling; off restores the `exists((…{prop: $param}))` predicate Neo4j 5 rejects |
| childsync_foreach_projection | True | childsync null-overrides child fields in the parent SET, creates children in FOREACH, emits a valid childtoparent arrow; off restores the UNWIND path Neo4j rejects |
