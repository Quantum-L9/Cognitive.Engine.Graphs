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
| spec_declared_candidate_id | True | `handle_match` identifies candidates by the domain's declared id property (sync `idproperty`, else first required property) instead of a hardcoded `entity_id`; since RU-002 also outcome edges (sync `outcome` + `outcomes`, label-scoped to the fingerprint direction or every candidate label), legacy `resolve`, `enrich` by `entity_ids` and admin `evaluate`; off restores `entity_id` only |
| match_dimension_projection | True | `handle_match` RETURNs `{<alias>: <alias>, ...} AS dimension_scores` for the active dimensions (RU-001 / CEG-COR-001); off restores `RETURN candidate, score` |
| gate_where_row_scope | True | `handle_match` emits `WITH *` between traversal clauses and the gate `WHERE`, so a trailing `OPTIONAL MATCH` cannot swallow the gates (RU-011 / CEG-COR-033); off restores the bare `WHERE` |
| match_collapse_traversal_rows | True | `handle_match` collapses to one row per candidate (highest score, `head(collect(...))`) before `ORDER BY`/`LIMIT` (RU-036 / CEG-COR-007); off restores one row per traversal path |
| monoculture_weighted_contribution | True | Monoculture flag uses `weight × value` contributions from `ScoringAssembler.last_effective_weights` (RU-032 / CEG-COR-167); off restores raw dimension values |
| query_defaults_before_derived | True | `handle_match` backfills declared queryschema defaults before derived-parameter resolution (RU-016 / CEG-COR-171); off restores resolve-then-backfill |
| match_effective_weight_validation | True | Match weight ceiling checks the merged weights (`ScoringAssembler.merged_request_weights`) and rejects unknown keys (RU-009 / CEG-COR-018); off sums supplied keys only |
| resolve_requires_created_edge | True | Legacy `handle_resolve` raises ValidationError when the CREATE returned no row (RU-004 / CEG-COR-014); off reports `resolved` unconditionally |
| outcome_map_json_encoding | True | `handle_outcomes` writes `dimension_weights` via `engine.models.outcomes.encode_outcome_map` (JSON); counterfactual reads via `decode_outcome_map` (RU-005 / CEG-COR-056); off restores `str(dict)` |
| health_probe_domain_count_only | True | `handle_health` tenant-missing text carries a pack count, not domain ids (RU-013 / CEG-COR-074); off restores the id list |
| sync_count_from_write_result | True | `UNWINDMATCHSET` sync reports the matched row count (`RETURN count(n) AS synced_count`) and `unmatched_count` (RU-060 / CEG-COR-169); off reports `len(batch)` |
| admin_tenant_bound_domain | True | `handle_admin` binds `domain_id` to the packet tenant via `_bind_admin_domain` (RU-003 / CEG-COR-003); off restores the caller-chosen domain |
| pricealignment_float_division | True | pricealignment divides `toFloat(candidate.<p>) / toFloat($<q>)` so INTEGER-stored prices are not truncated (RU-017 / CEG-COR-006); off restores the legacy `candidate.<p> / $<q>` text |
| arbitration_zero_score_present | True | decision_arbitration constraints and the arbitration Pareto read a dimension via `dimension_scores[d]` -> `d` -> `d_score` with `is None` tests, so a 0.0 is a score (RU-018 / CEG-COR-025); off restores the `or` fall-through |
| arbitration_objective_direction | True | decision_arbitration Pareto honours `ParetoObjectiveSpec.direction: minimize` and trims the front to `ParetoConfigSpec.front_size_limit` by composite score (RU-019 / CEG-COR-026); off restores maximise-all, unbounded |
| learned_weights_tenant_keyed | True | `ScoringAssembler.load_learned_weights` reads `(:DimensionWeight {tenant: $tenant}) WHERE dw.dimension IN $names`, the keys `SignalWeightCalculator` writes (RU-022 / CEG-COR-030); off restores the legacy `dimension_name`/`domain_id` reader, which matches nothing |
| learned_weight_renormalization | True | Learned DimensionWeight multipliers are applied, then the non-penalised coefficients are rescaled to their pre-learning sum (C-22); a learned 0.0 is loaded and takes the penalty branch (RU-023 / CEG-COR-083); off restores multiply-only and the truthiness filter |
| signal_weight_proportion_ci | True | SignalWeightCalculator derives ci_width from the win-rate proportion (Agresti-Coull 95% CI / base win rate), so confidence grows with n for any lift (RU-027 / CEG-COR-116); off restores `sqrt(lift*(1-min(lift,1))/n)` (0.0 for lift >= 1) |
| propagation_single_application | True | ScorePropagator collapses the candidate's outcomes to one existence row before the DimensionWeight MATCH, so one event multiplies each `boost_factor` once, clamped to signal_weights [min_weight, max_weight] (RU-028 / CEG-COR-117); off restores factor^k, unbounded |
| drift_requires_both_windows | True | DriftDetector reports `insufficient_data` (with recent/baseline counts) when either window is empty instead of alerting on an empty baseline (RU-029 / CEG-COR-118); off restores both-empty only |

## Correctness remediation lane C (plan 0d84ec8)
| scoring_admission_check_enabled | True | Admission compiles each direction's scoring and withholds a pack whose scoring refuses to compile, references an unbound `$parameter`, or declares an HGKR option with no producer (RU-020); off = gate-only admission |
| admission_checks_full_match | True | Admission requires each direction's traversal to assemble and every `$parameter` in gates, traversal and scoring to be bound by `handle_match` (RU-039) |
| exclusion_endpoint_validation | True | Refuse an exclusion gate declaring `fromnode`/`tonode` or whose edge does not start at a candidate label of its direction(s), instead of compiling an always-true `NOT EXISTS` (RU-041) |
| gate_null_operands_from_predicate | True | A gate's `nullbehavior` applies to exactly the operands its predicate tests (per-side PASS, any-NULL FAIL); off restores the `candidate.<candidateprop>` / `$<queryparam>` wrap (RU-042) |
| composite_subgate_scoping | True | Subgates referenced by a composite contribute only through it (skipped at top level of compile_all_gates / compile_relaxed); a subgate scoped away from the compiled direction is refused (RU-043) |
| freshness_maxagedays_validated | True | Freshness `maxagedays` defaults to 1 only when absent; a value < 1 is refused at compile (RU-052) |

## Correctness remediation — lane D (plan 0d84ec8)
| prohibited_factor_full_coverage | True | Prohibited-factor check covers every gate property field, scoring dimension `candidateprop`/`queryprop` and sync-row keys at any depth, casefolded (RU-061 / CEG-COR-008); off restores the gate `candidateprop`/`queryparam` exact-case check |
| pii_redaction_recursive | True | `PIIHandler` detects PII inside lists/tuples with index-aware paths (`candidates[0].candidate.email`) and redacts/masks by copying the edited path, never mutating the input; `redact_response` reaches match candidates (RU-062 / CEG-COR-009); off restores dict-only traversal |
