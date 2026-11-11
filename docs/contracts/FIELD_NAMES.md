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
# L9 Field Name Contract

## Rule
ALL Pydantic model fields use **snake_case**. ALL YAML spec keys use **snake_case**.
There are NO aliases, NO camelCase, NO flatcase fields anywhere in the engine.

When accessing a Pydantic model attribute in Python, the attribute name is IDENTICAL
to the YAML key name. No transformation occurs.

> **Gate keys (RU-053 / CEG-COR-081).** The authority is the field list of
> `GateSpec` in `engine/config/schema.py`, and those names are flatcase where the
> schema says so: `candidateprop`, `queryparam`, `matchdirections`,
> `nullbehavior`. A gate key that is not a `GateSpec` field fails validation
> (`gate_spec_forbid_unknown_keys`); before that, a re-spelled key such as
> `null_behavior: fail` was silently ignored and the gate compiled as `pass`.

## Canonical Field Names (DomainSpec)

These are the EXACT attribute names. Use these and ONLY these.

### DomainSpec (top-level)
```python
domain_spec.domain  # DomainMeta
domain_spec.domain.id  # str — also the Neo4j database name
domain_spec.domain.name  # str
domain_spec.domain.version  # str
domain_spec.ontology  # OntologySpec
domain_spec.ontology.nodes  # list[NodeSpec]
domain_spec.ontology.edges  # list[EdgeSpec]
domain_spec.match_entities  # MatchEntitiesSpec
domain_spec.match_entities.candidate  # list[CandidateEntity]
domain_spec.match_entities.query  # list[QueryEntity]
domain_spec.traversal  # TraversalSpec
domain_spec.traversal.steps  # list[TraversalStep]
domain_spec.gates  # list[GateSpec]
domain_spec.scoring  # ScoringSpec
domain_spec.scoring.dimensions  # list[ScoringDimension]
domain_spec.scoring.aggregation  # str — "additive" or "multiplicative"
domain_spec.sync  # SyncSpec
domain_spec.sync.endpoints  # list[SyncEndpoint]
domain_spec.gds_jobs  # list[GDSJobSpec]
domain_spec.compliance  # ComplianceSpec
domain_spec.compliance.prohibited_factors  # list[str]
```


### GateSpec

```python
gate.name  # str — unique within the spec
gate.type  # GateType enum value
gate.candidateprop  # str | None — property name on the candidate node
gate.queryparam  # str | int | float | bool | None — query parameter name, or a literal operand
gate.matchdirections  # list[str] | None — directions this gate applies to (None = all)
gate.nullbehavior  # NullBehavior — "pass" (default) or "fail"
# Type-specific keys are also top-level GateSpec fields: operator, mapping, edgetype,
# maxagedays, subgates, pattern, candidateprop_min/_max/_start/_end,
# queryparam_min/_max/_start/_end. There is no nested `params` dict.
```


### ScoringDimension

```python
dim.type  # ScoringType enum value
dim.weight  # float
dim.field  # str — property name on candidate node
dim.query_param  # str — key in query dict
dim.params  # dict[str, Any] — scoring-type-specific parameters
```


### SyncEndpoint

Correction (QM.POC0.TENANT_ASSOCIATION_MAPPING.004, D-1): `SyncEndpointSpec`
keys are **flatcase** in the live schema (`engine/config/schema.py`), and the
YAML key equals the attribute name. The snake_case names this section used to
list do not exist. Only this section is corrected here; the rest of this file
is unchanged.

```python
endpoint.path  # str — e.g., "/v1/sync/facilities"; the last segment is the entity type
endpoint.method  # str — "POST" | "PATCH"
endpoint.targetnode  # str | None — node label (MUST sanitize before Cypher)
endpoint.targetedge  # str | None — relationship type (relationship endpoints)
endpoint.fromnode  # str | None — relationship source label
endpoint.tonode  # str | None — relationship target label
endpoint.idproperty  # str | None — unique key property of a node endpoint
endpoint.batchstrategy  # SyncStrategy — UNWINDMERGE | UNWINDMATCHSET
endpoint.taxonomyedges  # list[TaxonomyEdgeSpec] | None
endpoint.childsync  # list[ChildSyncSpec] | None
endpoint.fieldsupdated  # list[str] | None — PATCH
endpoint.refnamespace  # str | None — node endpoints only; ids are "<refnamespace>:<record id>"
endpoint.observedfrom  # list[ObservedAssociationSourceSpec] | None — relationship endpoints only
```

`ChildSyncSpec` / `TaxonomyEdgeSpec` (typed sync contract, 2026-11-11):

```python
child.field  # str — targetnode property declared list<map>; one child per element
child.targetnode  # str — child node label
child.targetid  # str — a REQUIRED scalar property of the item map (and of the child node)
child.edgetype  # str
child.edgedirection  # str — "parenttochild" | "childtoparent"
taxonomy.field  # str — targetnode property declared scalar (one edge) or list<scalar> (one edge per element)
taxonomy.edgetype  # str
taxonomy.targetlabel  # str
taxonomy.targetid  # str — scalar property of targetlabel
```

A row carries a childsync field as a JSON array of objects, never a
JSON-encoded string; CEG does not parse strings into structures.

`PropertySpec` shape keys:

```python
prop.type  # PropertyType — string|int|float|bool|datetime|date|duration|enum|list|map
prop.items  # ItemSpec | None — element type; required iff type == "list"
prop.properties  # list[PropertySpec] | None — map keys; required iff type == "map"
item.type  # PropertyType — never "list"
item.values  # list[str] | None — enum items
item.properties  # list[PropertySpec] | None — required iff item.type == "map"
```

`ObservedAssociationSourceSpec` (unknown keys refused):

```python
source.provider  # str — ^[a-z][a-z0-9_-]{1,31}$
source.fromobjecttype  # str — provider-native object type of fromnode records
source.toobjecttype  # str — provider-native object type of tonode records
source.associationcategory  # str — ^[A-Z][A-Z0-9_]{0,63}$
source.associationtypeid  # int — strict, >= 1
```

### EdgeSpec

```python
edge.type  # str
edge.from_  # str — YAML key `from`
edge.to  # str
edge.direction  # EdgeDirection
edge.category  # EdgeCategory
edge.managedby  # ManagedByType
edge.evidencerelevance  # float | None — approved relevance in [0, 1]; scoring honours it at -reviewed/-production
```


### TraversalStep

```python
step.alias  # str — Cypher variable alias
step.pattern  # str — MATCH or OPTIONAL MATCH pattern
step.match_directions  # list[str] — which directions include this step
```


## WRONG (will crash)

```python
# ❌ These DO NOT EXIST — agents must never generate these
domain_spec.matchentities  # WRONG: missing underscore
domain_spec.match_entities.candidates  # WRONG: not plural 's'... wait yes it is
domain_spec.nodelabels  # WRONG: not a field
domain_spec.matchdirections  # WRONG: not a top-level field
gate.field  # WRONG: field is called 'candidateprop'
gate.null_semantics  # WRONG: field is called 'nullbehavior'
gate.null_behavior  # WRONG: field is called 'nullbehavior' (as a YAML key it fails validation)
dim.computation_type  # WRONG: field is called 'type'
endpoint.target_node  # WRONG: the live key is flatcase `targetnode`
endpoint.id_property  # WRONG: the live key is flatcase `idproperty`
```


## Enforcement

- Ruff TID251 cannot catch this (it's semantic, not import-based)
- `tools/audit_engine.py` checks for known wrong patterns
- Integration tests must instantiate DomainSpec from real YAML and access all fields

```

## Null Semantics Are Per-Gate (CONTRACT-14)

Every gate declares `nullbehavior` (`NullBehavior.PASS` by default, or `FAIL`). This is a
per-gate decision, not a global setting, and the compiler applies it — callers never
handle NULL themselves.

| `nullbehavior` | Compiled predicate |
|---|---|
| `pass` | `(candidate.prop IS NULL OR <predicate>)` |
| `fail` | `<predicate>` — a NULL candidate is rejected |

The policy applies to exactly the operands the predicate tests — the candidate
properties and declared query parameters it reads, never a literal operand and
never a related node's property (traversal / exclusion `candidateprop`). Under
`pass` a NULL operand imposes no constraint on its own side (range and
temporalrange bounds are null-handled per side); under `fail` any NULL tested
operand rejects (`gate_null_operands_from_predicate`, RU-042).

The wrapping happens in `engine/gates/null_semantics.py`. Do not hand-write
`IS NULL OR` into a gate's `compile_where()`; set `nullbehavior` in the spec instead.
