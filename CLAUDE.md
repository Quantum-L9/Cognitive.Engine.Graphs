<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [agent-rules]
tags: [L9_TEMPLATE, agent-rules, claude]
owner: platform
status: active
/L9_META -->

# CLAUDE.md — L9 Graph Cognitive Engine

@AGENTS.md

## Design Principles

1. **Domain spec is the single source of truth** — all behavior flows from YAML. No hardcoded business logic.
2. **Gate-then-score in Cypher, not Python** — matching is a single Cypher query. No post-filtering.
3. **Engine owns logic, chassis owns HTTP** — handlers receive `(tenant, payload)` → `dict`.
4. **Additive by default** — new capabilities as new files. Feature flags control activation.
5. **Explicit over implicit** — managed state (`EngineState`), bounded caches (`TTLCache`), validated inputs, bounded outputs ([0, 1]).
6. **Mechanism/policy separation** — engine proves mechanisms via tests. Operator activates via flags.

## Code Style Examples

```python
# ✅ GOOD — sanitized label, parameterized values, explicit types
from engine.utils.security import sanitize_label

async def query_candidates(driver: GraphDriver, spec: DomainSpec) -> list[dict[str, Any]]:
    label = sanitize_label(spec.targetnode)
    cypher = f"MATCH (n:{label}) WHERE n.active = $active RETURN n LIMIT $limit"
    return await driver.execute_query(cypher, {"active": True, "limit": settings.max_results})

# 🚫 BAD — unsanitized label, hardcoded limit, no type hints
async def query_candidates(driver, spec):
    cypher = f"MATCH (n:{spec.targetnode}) WHERE n.active = true RETURN n LIMIT 25"
    return await driver.execute_query(cypher)
```

```python
# ✅ GOOD — exception message in variable, explicit union, feature-flagged
def validate_weights(weights: dict[str, float] | None = None) -> None:
    if not settings.score_clamp_enabled:
        return
    if weights and sum(weights.values()) > 1.0:
        msg = f"Weight sum {sum(weights.values()):.4f} exceeds 1.0 ceiling"
        raise ValidationError(msg)

# 🚫 BAD — f-string in raise, implicit Optional, no flag gate
def validate_weights(weights: dict = None):
    if weights and sum(weights.values()) > 1.0:
        raise ValueError(f"Weight sum {sum(weights.values())} too high")
```

```python
# ✅ GOOD — gate type extends BaseGate, registered in enum
class ProximityGate(BaseGate):
    """Gate that filters by graph distance."""
    def compile_where(self, spec: GateSpec, domain: DomainSpec) -> str:
        field = sanitize_label(spec.candidateprop)
        return f"candidate.{field} <= $max_distance"

# 🚫 BAD — standalone function, no BaseGate, no sanitization
def proximity_gate(spec, domain):
    return f"candidate.{spec.candidateprop} <= {spec.threshold}"
```

## Boundaries

### ✅ Always
- Check the **Capability Registry** before building (see `.claude/rules/capability-registry.md`)
- Run `make lint` before committing
- Gate behavioral changes with feature flags in `engine/config/settings.py`
- Use `sanitize_label()` on all Cypher label interpolation
- Route Neo4j through `GraphDriver.execute_query()` — never raw sessions

### ⚠️ Ask Before
- Creating new top-level directories
- Modifying handler signatures in `engine/handlers.py`
- Changing `engine/config/schema.py` (affects all domain specs)
- Adding new action handlers
- Architectural changes to boot lifecycle

### 🚫 Never
- Import FastAPI/Starlette/uvicorn in `engine/`
- Use `eval()`, `exec()`, `pickle.load()`
- Interpolate values (not labels) into Cypher — use `$params`
- Log PII values
- Create unbounded caches
- Redefine PacketEnvelope, TenantContext, or ExecuteRequest

## Imports

```python
@docs/L9_Platform_Architecture.md
@docs/L9_AI_Constellation_Infrastructure_Reference.md
@docs/SEL4_UPGRADES.md
```

## Contract Docs

Load the specific doc for the subsystem you are about to touch — do not bulk-load.

| Subsystem | Docs |
|---|---|
| `engine/gates/`, `engine/config/schema.py` | `docs/contracts/FIELD_NAMES.md`, `docs/contracts/CYPHER_SAFETY.md`, `docs/contracts/BANNED_PATTERNS.md`, `docs/contracts/PYDANTIC_YAML_MAPPING.md`, `docs/contracts/BIDIRECTIONAL_MATCHING.md` |
| `engine/handlers.py`, `chassis/` | `docs/contracts/HANDLER_PAYLOADS.md`, `docs/contracts/METHOD_SIGNATURES.md`, `docs/contracts/DEPENDENCY_INJECTION.md`, `docs/contracts/RETURN_VALUES.md` |
| `engine/packet/` | `docs/contracts/PACKET_ENVELOPE_FIELDS.md`, `docs/contracts/SHARED_MODELS.md`, `docs/contracts/DELEGATION_PROTOCOL.md`, `docs/contracts/PACKET_TYPE_REGISTRY.md` |
| `tests/` | `docs/contracts/TEST_PATTERNS.md`, `docs/contracts/ERROR_HANDLING.md` |
| `engine/compliance/` | `docs/contracts/OBSERVABILITY.md`, `docs/contracts/MEMORY_SUBSTRATE_ACCESS.md`, `docs/contracts/PROHIBITED_FACTORS.md`, `docs/contracts/PII_HANDLING.md` |
| `domains/`, spec versioning | `docs/contracts/DOMAIN_SPEC_VERSIONING.md`, `docs/contracts/FEEDBACK_LOOPS.md`, `docs/contracts/NODE_REGISTRATION.md` |
| `.env.template`, env naming | `docs/contracts/ENV_VARS.md` |
| `engine/config/settings.py`, feature gating | `docs/contracts/FEATURE_FLAG_DISCIPLINE.md` |
| `engine/scoring/`, `engine/boot.py` | `docs/contracts/SCORING_WEIGHT_CEILING.md` |
| `engine/kge/` | `docs/contracts/KGE_EMBEDDINGS.md` |
| Any new tracked file | `docs/contracts/L9_META_HEADERS.md` |

## References

Detailed reference material loads automatically from `.claude/rules/` when you edit relevant files:
- **Contracts** → `.claude/rules/contracts.md` (all 24 contracts)
- **Feature Flags** → `.claude/rules/feature-flags.md`
- **Subsystems** → `.claude/rules/subsystems.md` (directory structure, handler registry, dependency map)
- **Capability Registry** → `.claude/rules/capability-registry.md` (18 existing capabilities)
- **Code Routing** → `.claude/rules/routing.md` (where to put code)
- **System State** → `.claude/rules/system-state.md` (open PRs, dormant subsystems)
