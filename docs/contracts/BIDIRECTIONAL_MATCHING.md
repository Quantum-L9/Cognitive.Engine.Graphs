<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts, gates, matching]
owner: engine-team
status: active
/L9_META -->

# Bidirectional Matching Contract

**Enforces:** `CONTRACT-15` (`contracts/contract_15.yaml`)
**Closes:** Agents hand-writing direction-specific branches inside gate implementations

## Rule

Gate implementations are **direction-unaware**. The compiler decides what a gate means
when the match direction reverses. Two spec fields control this:

| Field | Type | Effect |
|---|---|---|
| `invertible` | `bool` (default `False`) | Swaps the candidate property and query parameter roles |
| `matchdirections` | `list[str] \| None` (default `None`) | Restricts the gate to the listed directions; `None` = all directions |

## Direction scoping

`GateCompiler` (`engine/gates/compiler.py`) skips any gate whose `matchdirections` does
not contain the active `match_direction`:

```python
if gate.matchdirections and match_direction not in gate.matchdirections:
    continue
```

A gate with `matchdirections: null` fires in every direction. This same scoping applies
to scoring dimensions — see `FIELD_NAMES.md`.

## Inversion

`invertible: true` flips which side of the predicate holds the collection. For an
`enum_map` gate:

```python
if gate.invertible:
    return f"${gate.queryparam} IN candidate.{prop}"
return f"candidate.{prop} IN ${gate.queryparam}"
```

Read plainly: the non-inverted form asks *"is the candidate's single value in the query's
list?"*; the inverted form asks *"is the query's single value in the candidate's list?"*

## Correct

```yaml
gates:
  - name: material_compatibility
    type: enum_map
    candidateprop: accepted_materials    # candidate holds a list
    queryparam: material_code            # query holds one value
    invertible: true

  - name: supplier_only_freshness
    type: freshness
    candidateprop: last_updated
    matchdirections: [supplier_to_buyer]  # never fires buyer_to_supplier
```

## Wrong

```python
# WRONG — direction logic inside a gate type
def compile_where(self, spec, domain, direction):
    if direction == "buyer_to_supplier":
        return f"candidate.{spec.candidateprop} IN ${spec.queryparam}"
    return f"${spec.queryparam} IN candidate.{spec.candidateprop}"
```

Gate types receive no direction argument. If a gate needs different behavior per
direction, express it with `invertible` or `matchdirections` in the spec, or declare two
gates each scoped to one direction.

## Verified by

- `tests/contracts/test_contracts.py::TestContract15BidirectionalMatching`
- `tests/unit/` (gate compilation)
