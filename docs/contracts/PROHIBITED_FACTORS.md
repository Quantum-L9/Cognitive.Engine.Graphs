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
`ComplianceEngine` (`engine/compliance/engine.py`) calls it on three surfaces:

| Surface | Method | Checks |
|---|---|---|
| Gate compilation | `validate_gate()` | `gate.candidateprop`, `gate.queryparam` |
| Sync endpoint definition | `validate_sync_endpoint()` | endpoint field mappings, `idproperty` |
| Sync batch payload | audit path | every field name in the inbound batch |

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
    audit_on_violation: true
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

With `audit_on_violation: true`, every blocked attempt is recorded through the audit
path before the exception propagates. A rejected spec leaves a trail; it does not fail
silently.

## Verified by

- `tests/contracts/test_contracts.py::TestContract10ProhibitedFactors`
- `tests/compliance/`
