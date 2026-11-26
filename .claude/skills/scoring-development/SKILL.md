---
name: new-scoring
description: Add a new scoring computation to the CEG engine
---

# New Scoring Computation Development

## Steps

1. **Read existing computations**: Open `engine/scoring/assembler.py` — study the `_compile_*` methods
2. **Read ComputationType enum**: In `engine/config/schema.py`
3. **Add the computation method** in `ScoringAssembler`:
   - Method name: `_compile_{computationname}(self, dim: ScoringDimensionSpec) -> str`
   - Return a Cypher expression string
   - Expression MUST be wrapped in `_clamp_expression()` (Contract 22)
   - Reference only declared fields via `sanitize_label()` (Contract 9)
4. **Register** in `ComputationType` StrEnum
5. **Write tests** verifying:
   - Output is valid Cypher
   - Clamped to [0, 1] at boundaries (0.0 and 1.0)
   - Handles missing/null candidate properties correctly
6. **Run `make test-unit` and `make lint`**

## Clamping Rule

Every scoring expression MUST be clamped. Use the existing helper:
```python
expr = f"candidate.{field} / $max_value"
return self._clamp_expression(expr)  # Wraps in CASE WHEN > 1.0 THEN 1.0 ...
```

## Weight Ceiling

The engine declares no scoring dimensions and no default weights of its own:
do not add a weight to Settings. Weights belong to the domain pack, and a
domain's default dimension weights must sum to ≤ 1.0 per match direction,
checked when the spec loads (`docs/contracts/SCORING_WEIGHT_CEILING.md`).
