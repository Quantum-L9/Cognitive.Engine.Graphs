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

Default scoring weights sum to **at most 1.0**, and the sum is asserted at startup. A
misconfigured deployment fails to boot rather than silently emitting scores outside
`[0, 1]`.

## Defaults

`engine/config/settings.py`:

| Setting | Default |
|---|---|
| `w_structural` | 0.30 |
| `w_geo` | 0.25 |
| `w_reinforcement` | 0.20 |
| `w_freshness` | 0.10 |
| **Sum** | **0.85** |

The 0.15 headroom is deliberate — it is the budget for an additional dimension (KGE,
causal, persona) without breaching the ceiling.

## Startup assertion

`engine/boot.py`:

```python
_WEIGHT_CEILING = 1.0

def _assert_default_weight_sum() -> None:
    weight_sum = settings.w_structural + settings.w_geo + settings.w_reinforcement + settings.w_freshness
    if weight_sum > _WEIGHT_CEILING + _WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"... exceeding {_WEIGHT_CEILING}")
    logger.info("W1-02: Default weight sum validated: %.4f <= %.1f", weight_sum, _WEIGHT_CEILING)
```

A float tolerance is applied so that weights summing to exactly 1.0 do not trip on
binary-representation error.

## Adding a dimension

Take the weight from the headroom or rebalance the existing four. Do not add on top.

## Correct

```bash
# 0.30 + 0.25 + 0.20 + 0.10 = 0.85, plus a 0.15 KGE dimension = 1.00
W_STRUCTURAL=0.30
W_GEO=0.25
W_REINFORCEMENT=0.20
W_FRESHNESS=0.10
```

## Wrong

```bash
# WRONG — 1.30 total; boot raises ValueError
W_STRUCTURAL=0.50
W_GEO=0.50
W_REINFORCEMENT=0.20
W_FRESHNESS=0.10
```

## Per-request weights

Weights supplied in a `match` payload are validated separately by the scoring assembler
when `score_clamp_enabled` is set. The startup assertion covers **defaults only** — it
cannot see request-time overrides.

## Verified by

- `tests/contracts/test_contracts.py::TestContract22ScoringWeightCeiling`
  - `_assert_default_weight_sum` exists and `_WEIGHT_CEILING == 1.0`
  - shipped defaults are within the ceiling
  - the assertion raises when the sum is exceeded
