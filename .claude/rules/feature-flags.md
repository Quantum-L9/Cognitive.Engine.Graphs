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
| strict_tenant_database | True | Require explicit `database=` on GraphDriver calls; no implicit 'neo4j' fallback (W7-01; default flipped on by DB-STRICT-01 after the 2026-10-22 caller audit — `STRICT_TENANT_DATABASE=false` restores the fallback) |
| tenant_allowlist_required_in_prod | True | Under `l9_env == prod` an unset `TENANT_ALLOWLIST` denies every tenant (CEG-AUTH-01); `TENANT_ALLOWLIST_REQUIRED_IN_PROD=false` restores admit-with-warning |
| require_sdk_chassis_in_prod | False | Fail startup if `L9_CHASSIS != sdk` when `l9_env == prod` (W7-02) |

## Constellation Seam (EIE ↔ Gate ↔ CEG)
| Flag | Default | Purpose |
|------|---------|---------|
| health_commercial_mode_enabled | False | Tier entitlements, upgrade prompts and conversion events on the health surface; off = full capabilities, no funnel. The request's `tier` never grants (HEALTH-02 / COMM-01 / COMM-02) |
| health_entitled_tier | `seed` | Server-owned tier used only while commercial mode is on |
| health_batch_max_entities / health_batch_cost_ceiling_usd | 100 / 10.0 | Server-owned ceilings for `health_batch_assess`; a request may lower, never raise (BATCH-01) |
| packet_store_enabled / packet_store_dsn | False / unset | PacketStore persistence, read through Settings (PERSIST-01; env names unchanged) |
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
| gate_explicit_nullbehavior | True | A gate's explicitly declared `nullbehavior` wins over the per-type default (C-14); off restores the override of an explicit `pass` on boolean/freshness/traversal gates |
| spec_declared_candidate_id | True | `handle_match` identifies candidates by the domain's declared id property (sync `idproperty`, else first required property) instead of a hardcoded `entity_id`; off restores `entity_id` only |

## Correctness remediation — lane D (plan 0d84ec8)
| Flag | Default | Purpose |
|------|---------|---------|
| prohibited_factor_full_coverage | True | Prohibited-factor check covers every gate property field, scoring dimension `candidateprop`/`queryprop` and sync-row keys at any depth, casefolded (RU-061 / CEG-COR-008); off restores the gate `candidateprop`/`queryparam` exact-case check |
| pii_redaction_recursive | True | `PIIHandler` detects PII inside lists/tuples with index-aware paths (`candidates[0].candidate.email`) and redacts/masks by copying the edited path, never mutating the input; `redact_response` reaches match candidates (RU-062 / CEG-COR-009); off restores dict-only traversal |
| compliance_audit_soft_sink | True | Compliance audit buffer bounded by `compliance_buffer_max` (oldest dropped, count logged, every entry still in the `l9.audit` log), JSONB metadata bound as JSON text, and a request-path flush failure in `handle_match`/`handle_sync` logged instead of raised (RU-065 / CEG-COR-052/053); off restores unbounded retention and propagation |
| prohibited_factor_violation_audit | True | Every prohibited-factor violation raised through `ComplianceEngine` first emits one CRITICAL `denied` audit entry (regimes, subject, field name — never a value) when the domain's `auditonviolation` is true (RU-063 / CEG-COR-050); off emits none |
| pii_declared_fields_enforced | True | With `compliance.pii.enabled`, declared `fields` + `candidatefields` are PII by exact case-insensitive key at any depth and every detected field is transformed per `handling` (hash → SHA-256 digest, redact → `[REDACTED]`, encrypt/tokenize fail closed to `[REDACTED]`) (RU-064 / CEG-COR-051); requires `pii_redaction_recursive`; off restores built-in inference with key deletion |
| causal_attribution_unique_touchpoints | True | Attribution collapses trace rows to unique touchpoint ids (minimum-distance row kept, its ages feed temporal decay) so weights sum to 1.0 on diamonds/parallel edges (RU-067 / CEG-COR-055); off restores per-path-row weighting |
| causal_edge_declared_identity | True | `CausalCompiler` matches source/target by declared identity (`outcome_id` for the outcome label, else sync `idproperty` / first required property, else `entity_id`), joins predicates under one WHERE with `$confidence_threshold`, and MERGEs on the relationship type with ON CREATE/ON MATCH SET; `AttributionCalculator` reads each touchpoint's declared id per label (RU-068 / CEG-COR-057, PO-04); off restores the legacy Cypher |
| causal_validator_batch_edge_type | True | `CausalEdgeRuntimeValidator` resolves a `CausalSubgraphSpec` edge's `temporal_validation` from the batch `edge_type`, so sync records (no `edge_type` key) are checked (RU-069 / CEG-COR-058); off restores the record-key lookup |
| safe_eval_reject_keywords | True | `safe_eval` raises `ValueError` for a call with keyword arguments instead of silently dropping them (RU-076 / CEG-COR-125); off restores the silent drop |
| causal_serializer_variable_depth | True | `CausalSubgraphSerializer` uses `[:T1|T2*1..N]` with N = `max_depth` clamped to [1, `causal.chain_depth_limit`] so match explanations reach the requested depth (RU-074 / CEG-COR-121); off restores the one-hop `[r:T]` pattern |
