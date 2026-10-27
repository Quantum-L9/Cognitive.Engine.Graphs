<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts, compliance, prohibited-factors]
owner: engine-team
status: active
/L9_META -->

# Prohibited Factors Contract

**Enforces:** `CONTRACT-10` (`contracts/contract_10.yaml`)
**Closes:** Agents compiling gates or sync mappings that reference protected attributes

## Rule

Protected attributes are blocked at **compile time**, not at query time. A domain spec
that references a prohibited field fails gate compilation with a `ValueError` — it never
reaches Neo4j, so there is no runtime path that can leak the factor.

## Where the block happens

`ProhibitedFactorValidator` (`engine/compliance/prohibited_factors.py`) is constructed
from `domain_spec.compliance.prohibitedfactors` and owns a `blocked_fields` set.
`ComplianceEngine` (`engine/compliance/engine.py`) calls it on these surfaces, before any
Cypher is compiled or executed (`handle_match` before the match query, `handle_sync`
before the sync query):

| Surface | `ComplianceEngine` method | Validator method | Checks |
|---|---|---|---|
| Gate compilation (`handle_match`) | `validate_gates()` | `validate_gate()` | `candidateprop`, `candidateprop_min`/`_max`/`_start`/`_end`, `queryparam` (str only — a scalar literal is an operand, not a field), `queryparam_min`/`_max`/`_start`/`_end` |
| Scoring compilation (`handle_match`) | `validate_scoring()` | `validate_scoring_dimension()` | dimension `candidateprop`, `queryprop` |
| Sync endpoint definition (`handle_sync`) | `validate_sync_fields()` (via `check_sync_request()`) | `is_blocked()` | `fieldsupdated`, `idproperty` |
| Sync batch payload (`handle_sync`) | `check_sync_request()` | `validate_sync_row()` | every key at any depth of each row (nested dicts and lists of dicts, e.g. `childsync` collections) |

Field names are compared **casefolded** (`Race` matches `race`). The error names the
gate, dimension or key, never a value. `PROHIBITED_FACTOR_FULL_COVERAGE=false` restores the
pre-RU-061 check (gate `candidateprop`/`queryparam` and top-level sync keys only,
exact-case) for rollback while a spec is corrected.

Not covered (decision DEC-24 / R3-DEC-03, pending): raw-Cypher spec fields (gate
`cypheroverride`/`condition`/`pattern`, scoring `customcypher` `expression`, traversal
steps) and separator variants (`national_origin` vs `nationalorigin`).

If `prohibitedfactors.enabled` is `False` or the section is absent, `blocked_fields` is
empty and every check short-circuits — the mechanism ships dormant.

## Spec shape

```yaml
compliance:
  prohibitedfactors:
    enabled: true
    blockedfields:
      - race
      - ethnicity
      - religion
      - gender
      - age
      - disability
      - familial_status
      - national_origin
    auditonviolation: true
```

## Correct

```yaml
gates:
  - name: capacity_threshold
    type: threshold
    candidateprop: monthly_capacity_tons   # operational attribute
    queryparam: required_capacity
```

## Wrong

```yaml
gates:
  - name: demographic_filter
    type: enum_map
    candidateprop: ethnicity               # WRONG → compile-time ValueError
    queryparam: target_ethnicity
```

The failure is loud and early:

```
ValueError: Gate 'demographic_filter' references prohibited field 'ethnicity'.
Blocked fields: {'race', 'ethnicity', ...}
```

## Audit

With `auditonviolation: true` (the schema default), every blocked attempt raised through
`ComplianceEngine` — gates, scoring dimensions, sync endpoint, sync rows — first emits
one audit entry (`AuditLogger.log_violation`: severity `critical`, `outcome: denied`,
`compliance_tags` = the domain's regime names, `resource` naming the gate / dimension /
endpoint / row path and the blocked field, `metadata.blocked_field` = the field name —
never a value), then re-raises the same `ValueError` (a `ProhibitedFactorViolationError`
subclass). A rejected spec leaves a trail; it does not fail silently. The entry rides the
next audit flush. `PROHIBITED_FACTOR_VIOLATION_AUDIT=false` turns the entry off.

## Verified by

- `tests/contracts/test_contracts.py::TestContract10ProhibitedFactors`
- `tests/compliance/`
