<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts, scoring, weights]
owner: engine-team
status: active
/L9_META -->

# Scoring Weight Ceiling Contract

**Enforces:** `CONTRACT-22` (`contracts/contract_22.yaml`)
**Closes:** Agents adding a scoring dimension whose weight pushes the composite score above 1.0

## Rule

A domain's scoring dimensions and their default weights are the domain's declaration
(`scoring.dimensions[*].defaultweight` in its `spec.yaml`). For every match direction the
default weights of the dimensions that score in it sum to **at most 1.0**. The check runs
when the spec loads, so a pack that breaches the ceiling is refused rather than emitting
scores outside `[0, 1]`.

The engine declares no scoring dimensions and no default weights of its own. The former
engine-wide settings `w_structural`, `w_geo`, `w_reinforcement` and `w_freshness` (and the
boot-time assertion over their sum) named one pack's dimensions, reached no scoring path,
and were retired (CEG domain-neutrality Stage 2).

## Where it is checked

`engine/config/schema.py` — `DomainSpec` cross-reference validation, with
`domain_strict_validation` on (the default):

- dimensions are bucketed by `matchdirections` (an unscoped dimension scores in every
  direction);
- with `weight_ceiling_includes_unscoped` (default on, RU-044) each declared direction
  carries the unscoped weight on top of its own;
- a bucket over `1.0` (with a `1e-9` float tolerance) raises
  `ValueError("Scoring dimension default weights sum to … for direction …, exceeding 1.0")`.

`engine/config/loader.py` — the offline authoring validator (`validate_authoring_file`)
reports the same breach as the `W1-01-weight-sum` FAIL.

## Adding a dimension

Take the weight from the direction's headroom or rebalance that domain's other dimensions.
Do not add on top.

## Correct

```yaml
scoring:
  dimensions:
  - {name: humidity_fit, computation: candidateproperty, candidateprop: humidity_pct,
     weightkey: w_humidity, defaultweight: 0.6}
  - {name: turnover_fit, computation: inverselinear, candidateprop: turnover_days,
     weightkey: w_turnover, defaultweight: 0.4}   # 0.6 + 0.4 = 1.0
```

## Wrong

```yaml
# WRONG — 1.2 for the direction; the spec is refused at load
scoring:
  dimensions:
  - {name: humidity_fit, computation: candidateproperty, candidateprop: humidity_pct,
     weightkey: w_humidity, defaultweight: 0.6}
  - {name: turnover_fit, computation: inverselinear, candidateprop: turnover_days,
     weightkey: w_turnover, defaultweight: 0.6}
```

## Per-request weights

Weights supplied in a `match` payload are validated separately (`_validate_match_weights`,
and with `match_effective_weight_validation` the merged per-dimension weights scoring will
apply) when `score_clamp_enabled` is set. The spec-load check covers declared defaults
only — it cannot see request-time overrides.

## Verified by

- `tests/contracts/test_contracts.py::TestContract22ScoringWeightCeiling`
  - no engine-wide weight set or boot assertion exists
  - a spec within the ceiling is accepted; one exceeding it is refused
- `tests/unit/test_wave1_invariants.py::TestW101DomainCrossRef` (per-direction buckets, RU-044)
