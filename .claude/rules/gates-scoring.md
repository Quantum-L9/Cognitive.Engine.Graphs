---
paths:
  - "engine/gates/**/*.py"
  - "engine/scoring/**/*.py"
  - "engine/traversal/**/*.py"
---
# Gates, Scoring & Traversal Rules

## Gate Compilation (Contract 13, 14, 15)
- 10 gate types: range, threshold, boolean, composite, enummap, exclusion, selfrange, freshness, temporalrange, traversal
- Compilation order: Traversal → Gates → Scoring (gate WHERE depends on traversal MATCH)
- Every gate declares `nullbehavior: pass | fail` (default `pass`) — compiler handles it, callers don't. The key is `nullbehavior`; an unknown gate key such as `null_behavior` fails validation (`gate_spec_forbid_unknown_keys`, RU-053)
- Gates with `invertible: true` swap candidate ↔ query for bidirectional matching
- `validate_gates()` pre-pass (W1-03): with `strict_null_gates=True`, `handle_match` runs it on the final resolved parameters for the direction's gates and rejects (ValidationError) a null gate parameter that is undeclared or a `required` queryschema field; a null optional (or derived) parameter follows the gate's `nullbehavior` and is logged (RU-010 / R1a-DEC-03)
- All labels MUST pass `sanitize_label()` before Cypher interpolation (Contract 9)

## Scoring (Contract 13, 22)
- 13 computation types: geodecay, lognormalized, communitymatch, inverselinear, candidateproperty, weightedrate, pricealignment, temporalproximity, customcypher, traversalalias, kge, variantdiscovery, ensembleconfidence
- Every dimension expression clamped to [0, 1] via `_clamp_expression()` (W1-02)
- A domain's default dimension weights sum ≤ 1.0 per match direction, checked when the spec loads (the engine has no weights of its own)
- When adding a dimension to a pack, REDUCE that pack's other defaults to maintain the ceiling
- Monoculture detection: flag candidates where one dimension > 70% of total score
- Calibration: `engine/scoring/calibration.py` verifies scores against expected ranges

## Traversal (Contract 13)
- `validate_traversal()` enforces 1 ≤ max_hops ≤ MAX_HOP_HARD_CAP (10)
- Direction must be OUTGOING, INCOMING, or BOTH
- Hard LIMIT MAX_RESULTS in Cypher regardless of caller input
- Parameter resolver raises ValidationError on failure when `param_strict_mode=True`

## Adding a New Gate Type
1. Create class in `engine/gates/types/all_gates.py` extending `BaseGate`
2. Implement `compile_where(spec, domain)` → Cypher WHERE string
3. Add to `GateType` enum in `engine/config/schema.py`
4. Add unit tests in `tests/unit/`
5. Sanitize ALL field references via `sanitize_label()`

## Adding a New Scoring Computation
1. Add `_compile_*` method in `engine/scoring/assembler.py`
2. Add to `ComputationType` enum in `engine/config/schema.py`
3. Expression MUST be wrapped in `_clamp_expression()` to guarantee [0, 1]
4. Add unit tests verifying clamping behavior at boundaries
