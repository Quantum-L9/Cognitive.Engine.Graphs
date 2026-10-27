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
```

RU-060 (`SYNC_COUNT_FROM_WRITE_RESULT`, default on): an `UNWINDMATCHSET`
endpoint updates only rows whose node already exists, so its statement ends
`RETURN count(n) AS synced_count` and the response reports that count plus
`unmatched_count`. `UNWINDMERGE` creates missing nodes; its `synced_count`
stays `len(batch)` and it carries no `unmatched_count`.


## admin

```python
class AdminPayload(BaseModel):
    subaction: Literal["list_domains", "get_domain", "init_schema", "trigger_gds"]
    domain_id: str | None = None  # Required for get_domain, init_schema, trigger_gds; must equal the packet tenant (RU-003)
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
tenant. `list_domains` and `delegate_capability` are unchanged.


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
