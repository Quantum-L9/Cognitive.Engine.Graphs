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
# L9 Handler Payload Contract

## Rule
Every action handler has a FIXED payload schema. Agents must validate
incoming payloads against these schemas before processing.

## match

> TASK-040 / ADR-106: prefer `engine.models.payloads.MatchRequest` / `MatchResponse` (payload-only; no transport fields; no ungoverned weights).
>
> RU-012 (`MATCH_REQUEST_CONTRACT_ADMISSION`, default on, R1a-DEC-04 "admit both"): the
> match action accepts both shapes. A payload carrying `contract_version` is admitted as
> `MatchRequest` against `DomainContract.from_spec(<tenant's pack>)` (its `domain` must be
> the tenant's, its `direction` declared, unknown and transport fields refused; any failure is
> a `ValidationError`) and mapped onto the legacy internals: `direction` -> `match_direction`,
> `query.facts` -> `query`, `top_n` -> `top_n`. `query_id`, `query_entity_ref`,
> `evidence_summary`, `governed_filters`, `projection_version`, `policy_ref` and
> `field_dictionary_version` are validated but not used by the match. A payload without
> `contract_version` is the legacy `MatchPayload` below, unchanged. The response is the legacy
> `MatchResponse` in both cases (`MatchResponse` emission is not implemented).
```python
class MatchPayload(BaseModel):
    query: dict[str, Any]  # Entity attributes to match against
    match_direction: str  # e.g., "buyer_to_seller"
    top_n: int = 10  # Max candidates to return (1-1000)
    weights: dict[str, float] = {}  # Override scoring dimension weights
    filters: dict[str, Any] = {}  # Additional Cypher filters


class MatchResponse(BaseModel):
    candidates: list[dict[str, Any]]
    query_id: str
    match_direction: str
    total_candidates: int
    execution_time_ms: float
```


## sync

```python
class SyncPayload(BaseModel):
    entity_type: str  # Must match a sync endpoint path
    batch: list[dict[str, Any]]  # 1-10000 entities per batch


class SyncResponse(BaseModel):
    status: Literal["success"]
    entity_type: str
    synced_count: int  # UNWINDMATCHSET: rows that matched an existing node (RU-060)
    unmatched_count: int | None = None  # UNWINDMATCHSET only: len(batch) - synced_count
    undeclared_properties: dict[str, int] | None = None  # present only when keys were dropped
```

**Typed rows (CEG validates the batch against the tenant's spec).** For a node
endpoint (`targetnode`), every row is checked against the properties its
`targetnode` declares before compliance logging and before any Cypher
(`SYNC_ROW_TYPE_VALIDATION_ENABLED`, default on). Types are JSON types, never
coerced: `string`, `int` (not bool), `float` (int or float, not bool, finite),
`bool`, `datetime` / `date` / `duration` (ISO-8601 strings), `enum` (a declared
value), `list<T>` (a JSON array whose every element is `T`, no nulls),
`map` (a JSON object checked against its declared `properties`). A childsync
field declared `list<map>` must be a JSON array of objects — a JSON-encoded
string is refused, not parsed. `required` applies to the endpoint's
`idproperty` and to map members; `nullable: false` refuses an explicit null.

Any defect refuses the whole batch — nothing is written — with
`SyncRowRejectedError` (a `ValidationError`, HTTP 422) whose message is
`Sync refused, nothing written: <n> defect(s): <defect>; <defect>; ...` and
whose `defects` attribute carries the same list. Every defect, every row:

```text
row 0 processed_polymers: expected list<map>, got string
row 1 processed_polymers[0]: expected map, got string
row 2 rate_per_ton: expected float, got string
row 3 processed_polymers[0].code: expected string (required), got missing
row 4 facility_id: expected string (required id), got missing
row 5 colour: expected no such declared property, got string   # only with SYNC_UNDECLARED_PROPERTY_REFUSAL_ENABLED
```

Values are never echoed (only their JSON type). Undeclared keys, with
`SYNC_UNDECLARED_PROPERTY_REFUSAL_ENABLED` off (default), are dropped — never
written — and counted by key path in `undeclared_properties`
(`{"colour": 2, "processed_polymers[].lot": 1}`). Only declared, storable
properties reach the node; a childsync child receives only the properties its
item map and its node both declare (a `managedby: static` child only when it
is created). Relationship endpoints (`targetedge`) keep their own row
contract (`engine/sync/generator.py::normalize_relationship_rows`).

RU-060 (`SYNC_COUNT_FROM_WRITE_RESULT`, default on): an `UNWINDMATCHSET`
endpoint updates only rows whose node already exists, so its statement ends
`RETURN count(n) AS synced_count` and the response reports that count plus
`unmatched_count`. `UNWINDMERGE` creates missing nodes; its `synced_count`
stays `len(batch)` and it carries no `unmatched_count`.


## outcomes

```python
class OutcomeFingerprint(BaseModel):
    active_dimensions: list[str] = []
    dimension_weights: dict[str, float] = {}  # stored as sorted-key JSON (RU-005)
    gates_passed: list[str] = []
    match_direction: str | None = None  # scopes the RESULTED_IN candidate lookup (RU-002)
    candidate_count: int = 0
    dimension_scores: dict[str, float] | None = None  # RU-006: per-dimension scores of the match
    was_selected: bool | None = None  # RU-006: whether the caller selected this candidate


class OutcomesPayload(BaseModel):
    match_id: str
    candidate_id: str
    outcome: Literal["success", "failure", "partial"]
    value: Any | None = None
    fingerprint: OutcomeFingerprint = OutcomeFingerprint()
```

RU-006 (`OUTCOME_SCHEMA_UNIFIED`, default on, R1a-DEC-02): `handle_outcomes` is
the canonical outcome writer. It creates one `feedbackloop.outcome_node_label`
node (default `TransactionOutcome`) carrying the fingerprint, plus
`dimension_scores` (JSON; null when omitted) and `was_selected`, which feed
`admin score_feedback` and `admin discover_weights`. A `dimension_scores` that
is not a map of finite numbers, or a non-boolean `was_selected`, raises
`ValidationError`. The sync `outcome` entity type (feedback-effective domains
only) writes each batch item — `match_id`, `chosen_candidate_id`, `outcome`,
optional `value` and `fingerprint` — through the same writer, mapping
`positive`/`negative`/`neutral` to `success`/`failure`/`partial`; items with any
other outcome or a malformed fingerprint are skipped.


## admin

```python
class AdminPayload(BaseModel):
    subaction: Literal["list_domains", "get_domain", "init_schema", "trigger_gds"]
    # Required for get_domain, init_schema, trigger_gds; must equal the packet tenant (RU-003)
    domain_id: str | None = None
    job_name: str | None = None  # Required for trigger_gds


class AdminResponse(BaseModel):
    # Varies by subaction — always a dict
    pass
```

RU-003 (`ADMIN_TENANT_BOUND_DOMAIN`, default on): the identity contract binds a
tenant to the domain pack with the same id, so every domain-bearing admin
subaction (`get_domain`, `init_schema`, `trigger_gds`, `calibration_run`,
`score_feedback`, `generate_calibration_pairs`, `domain_density_report`,
`auto_tune`, `discover_weights`, `trigger_kge`, `kge_status`, `erase_subject`,
`gds_status`, `gds_trigger`, `gds_health`, `emit_inference_feedback`,
`evaluate`) takes `domain_id` absent (where optional; it defaults to the
tenant) or equal to the packet tenant. Anything else raises `ValidationError`
before any domain load or query. `evaluate` runs its matches as the packet
tenant. `list_domains` is unchanged.

`delegate_capability` binds `source_tenant` to the packet tenant. It requires a
previously registered, active parent capability owned by that tenant; the
requested actions and domain remain a monotonic subset of that parent, and a
negative expiry is refused. Payload fields cannot create a root capability.
The target tenant is recorded while the child capability is derived, before it
is registered and hashed.


## health-assess (HEALTH-01)

The canonical health route. Gate owns the action name for `ceg`; it is the
only advertised way to reach `engine/health/api.py`. `admin` is never
advertised, so health is not routed through `admin` — the same three
subactions remain on `admin` only as a direct operator surface in dev.

```python
class HealthAssessPayload(BaseModel):
    subaction: Literal["health_assess", "health_batch_assess", "health_report"]
    domain: str | None = None  # HEALTH-05: absent or equal to the packet tenant; anything else is rejected
    # Remaining keys are the subaction's own request model in engine/health/health_schemas.py
    # (HealthAssessmentRequest, BatchHealthRequest) or, for health_report, entity_id.


class HealthAssessResponse(BaseModel):
    # The subaction's own response model (EntityHealthResponse, BatchHealthReport, HealthReport);
    # {"status": "disabled", ...} while HEALTH_API_ENABLED is off.
    pass
```

Any other `subaction` raises `ValidationError` (`action="health-assess"`);
nothing else `handle_admin` offers is reachable from this action.


## Validation Pattern

```python
async def handle_match(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    validated = MatchPayload.model_validate(payload)  # Raises ValidationError
    # Use validated.query, validated.match_direction, etc.
```

```

## Admin Subaction Registration (CONTRACT-23)

`handle_admin` dispatches on a `subaction` key. Two rules:

1. **Resolve it through `_require_key()`** — never `payload.get("subaction")`. A missing
   key must raise, not fall through to a default branch.
2. **Names are `snake_case`** — matching `[a-z][a-z0-9_]*`. No camelCase, no dots, no
   spaces.

```python
# ✅
subaction = _require_key(payload, "subaction", "admin", tenant)
if subaction == "trigger_gds_job":
    ...

# ❌
subaction = payload.get("subaction", "describe")   # silent default
if subaction == "triggerGDSJob":                   # not snake_case
```
