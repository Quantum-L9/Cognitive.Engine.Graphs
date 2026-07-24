---
name: gate-development
description: Add a new gate type to the CEG engine
---

# Gate Type Development

A gate compiles to a Cypher `WHERE` fragment. Gates are a **hard filter** — the
gate-then-score architecture forbids post-filtering in Python (Contract 13).

## Two implementations, both required

Gate logic lives in two places. Updating only one produces a gate that works at
top level but silently degrades inside a `composite` gate (or vice versa).

| File | Class/Method | Used by |
|------|--------------|---------|
| `engine/gates/compiler.py` | `GateCompiler._compile_<name>` | Production path — every top-level gate |
| `engine/gates/types/all_gates.py` | `<Name>Gate(BaseGate).compile()` | `CompositeGate` subgate recursion, via `GateRegistry` |

`tests/test_boot_and_registry.py::TestGateRegistry` asserts `GateRegistry._REGISTRY`
covers every `GateType` value, so skipping `all_gates.py` fails the suite.

## Steps

1. **Add the enum value** to `GateType` in `engine/config/schema.py`.
   Values are flatcase (`temporalrange`, not `temporal_range`).

2. **Add the compiler handler** in `engine/gates/compiler.py`:
   - Write `_compile_<name>(self, gate: GateSpec) -> str`
   - Register it in the `handlers` dict inside `_get_handler()`
   - Every property/label goes through `sanitize_label()` (Contract 9)
   - Return the raw predicate — `compile()` wraps NULL semantics for you

3. **Add the gate class** in `engine/gates/types/all_gates.py`:
   - Extend `BaseGate`, implement `compile(self) -> str`
   - Read config from `self.spec` (a `GateSpec`) and `self.domain_spec`
   - Use `self._prop_ref()` / `self._param_ref()` helpers
   - Raise `ValueError` with the gate name when a required spec field is missing

4. **Register the class** in `GateRegistry._REGISTRY` in `engine/gates/registry.py`
   and add it to the import block at the top.

5. **Export it** from `engine/gates/types/__init__.py`.

6. **Write tests** in `tests/unit/test_gates_all_types.py`:
   - Happy path: compiles to the expected Cypher
   - `null_behavior: pass` wraps the predicate in `IS NULL OR`
   - `null_behavior: fail` leaves the predicate bare
   - Missing required spec field raises `ValueError`
   - Injection attempt in a property name is rejected by `sanitize_label()`

7. **Run `make agent-check`**.

## Interfaces

```python
# engine/gates/types/all_gates.py
class BaseGate(ABC):
    def __init__(self, spec: GateSpec, domain_spec: DomainSpec): ...

    @abstractmethod
    def compile(self) -> str:
        """Cypher WHERE fragment, without NULL handling."""

    def _prop_ref(self, prop: str) -> str:   # -> "candidate.<prop>"
    def _param_ref(self, param: str) -> str: # -> "$query.<param>"
```

```python
# engine/gates/compiler.py
def _compile_<name>(self, gate: GateSpec) -> str:
    prop = sanitize_label(gate.candidateprop) if gate.candidateprop else "prop"
    return f"candidate.{prop} >= $query.{gate.queryparam}"
```

NULL wrapping is applied centrally by `GateCompiler.compile()` via
`_wrap_null_semantics()` (Contract 14). Do not hand-roll it in a gate.

## Contracts to read first

- `docs/contracts/FIELD_NAMES.md`
- `docs/contracts/METHOD_SIGNATURES.md`
- `docs/contracts/CYPHER_SAFETY.md`
- `docs/contracts/HANDLER_PAYLOADS.md`

## Anti-patterns (each caused a real audit finding)

- Flatcase field names in Python (C-1 → C-5) — spec **keys** are flatcase, Python
  identifiers are snake_case
- `eval()` for parameter computation (C-6, C-7)
- f-string interpolation of `LIMIT` / `SKIP` values (C-8)
- `str()` on a list to build a Cypher collection (GDS Louvain bug) — use `$params`
- Interpolating any *value* into Cypher — only sanitized labels may be interpolated
