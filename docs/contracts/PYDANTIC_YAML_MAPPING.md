<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts]
owner: platform
status: active
/L9_META -->

<!-- L9_TEMPLATE: true -->
# L9 Pydantic ↔ YAML Mapping Contract

## Rule
Pydantic field names are IDENTICAL to YAML keys. No aliases. No Field(alias=...).
No model_config with populate_by_name. The spelling is whatever the schema field
is (`engine/config/schema.py`); many are flatcase (`matchentities`,
`candidateprop`, `queryparam`, `nullbehavior`). Never re-spell a key: a gate key
that is not a `GateSpec` field fails validation (RU-053, flag
`gate_spec_forbid_unknown_keys`), where it used to be silently ignored.

## Example: spec.yaml ↔ DomainSpec

```yaml
# YAML (domains/plasticos_domain_spec.yaml)
domain:
  id: plasticos
  name: PlasticOS
  version: "1.0.0"

matchentities:             # ← the schema field name, verbatim
  candidate:
    - label: Facility
      matchdirection: buyer_to_seller

gates:
  - name: mfi_window
    type: range
    candidateprop_min: mfi_range_min   # ← GateSpec field names, verbatim
    candidateprop_max: mfi_range_max
    queryparam: mfi
    nullbehavior: pass                 # ← NOT null_behavior (unknown key → ValidationError)
```

```python
# Python (engine/config/schema.py)
class DomainSpec(BaseModel):
    domain: DomainMetadata
    matchentities: MatchEntitiesSpec  # ← SAME as YAML key
    gates: list[GateSpec]


class GateSpec(BaseModel):
    name: str
    type: GateType
    candidateprop_min: str | None = None
    candidateprop_max: str | None = None
    queryparam: str | int | float | bool | None = None
    nullbehavior: NullBehavior = NullBehavior.PASS  # ← SAME as YAML key
```


## BANNED Patterns

```python
# ❌ No aliases
class GateSpec(BaseModel):
    nullbehavior: NullBehavior = Field(alias="nullBehavior")  # BANNED


# ❌ No re-spelled keys (the YAML key must be the field name)
class DomainSpec(BaseModel):
    match_entities: MatchEntitiesSpec  # BANNED — the field is matchentities


# ❌ No camelCase
class DomainSpec(BaseModel):
    matchEntities: MatchEntitiesSpec  # BANNED
```

```
