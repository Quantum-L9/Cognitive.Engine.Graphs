<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts, feature-flags, settings]
owner: engine-team
status: active
/L9_META -->

# Feature Flag Discipline Contract

**Enforces:** `CONTRACT-21` (`contracts/contract_21.yaml`)
**Closes:** Agents shipping behavioral changes that activate on merge with no operator control

## Rule

Every behavioral change is gated by a boolean flag declared in
`engine/config/settings.py` and documented in `docs/FEATURE_GATES.md`. The engine proves
the mechanism; the operator decides the policy.

## Three requirements

1. **Declared in `Settings`** — flag name ends with `_enabled` and is annotated `bool`.
2. **Documented** — the flag (or its uppercase env var form) appears in
   `docs/FEATURE_GATES.md`.
3. **Defaults chosen deliberately** — new behavior generally ships `False`; hardening that
   restores an invariant may ship `True`.

## Correct

```python
# engine/config/settings.py
class Settings(BaseSettings):
    score_clamp_enabled: bool = True
    outcome_persistence_enabled: bool = False
```

```python
# engine/scoring/assembler.py
def validate_weights(weights: dict[str, float] | None = None) -> None:
    if not settings.score_clamp_enabled:
        return
    ...
```

Then add a row to `docs/FEATURE_GATES.md`:

| Capability | Env Var | Default | Status |
|---|---|---|---|
| Score Clamping | `SCORE_CLAMP_ENABLED` | `True` | active |

## Wrong

```python
# WRONG — new behavior on by default with no flag and no doc row
def assemble_scoring_clause(...):
    score = clamp(score, 0.0, 1.0)   # silently changes every existing domain's output
```

```python
# WRONG — flag typed as str, so `if settings.foo_enabled` is true for "false"
foo_enabled: str = "false"
```

## Naming

- Setting: `snake_case`, suffix `_enabled`
- Env var: the same name uppercased (`SCORE_CLAMP_ENABLED`)
- No `Field(alias=...)` — YAML/env keys equal Python field names (`NAME-001`)

## Verified by

- `tests/contracts/test_contracts.py::TestContract21FeatureFlagDiscipline`
  - flags are declared `bool`
  - `docs/FEATURE_GATES.md` exists
  - every `*_enabled` flag appears in that doc
