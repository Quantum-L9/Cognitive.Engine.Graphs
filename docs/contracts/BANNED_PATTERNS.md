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
# L9 Banned Patterns (Comprehensive)

Quick-reference for ALL agents. If you see yourself writing any of these, STOP.

## Imports
| ❌ Banned | ✅ Instead | Why |
|-----------|-----------|-----|
| `from fastapi import ...` (in engine/) | Register handlers in `engine/handlers.py` | Chassis owns HTTP |
| `from starlette import ...` | Nothing — chassis handles middleware | Chassis owns middleware |
| `import uvicorn` | Nothing — chassis runs uvicorn | Chassis owns ASGI |
| `from engine.api import ...` | `from engine.handlers import ...` | engine/api/ is deleted |

## Security
| ❌ Banned | ✅ Instead | Why |
|-----------|-----------|-----|
| `eval(expr)` | Dispatch table with explicit operators | Code injection |
| `exec(code)` | Never needed in engine | Code injection |
| `compile(code)` | Never needed in engine | Code injection |
| `f"MATCH (n:{label})"` without sanitize | `f"MATCH (n:{sanitize_label(label)})"` | Cypher injection |
| `f"... LIMIT {top_n}"` | `"... LIMIT $limit"` + params | Cypher injection |
| `pickle.loads()` | `json.loads()` | Deserialization attack |
| `yaml.load()` (without Loader) | `yaml.safe_load()` | YAML deserialization |

## Architecture
| ❌ Banned | ✅ Instead | Why |
|-----------|-----------|-----|
| Custom FastAPI routes | `register_handler("action", handler_fn)` | L9 chassis contract |
| Tenant resolution in engine | Receive `tenant` as handler arg | Chassis resolves tenant |
| CORS/auth/rate-limit in engine | Nothing — chassis provides | Chassis owns all HTTP concerns |
| `Depends(resolve_tenant)` | `tenant: str` handler parameter | No FastAPI in engine |
| Creating `engine/api/` directory | Don't | Chassis owns API surface |
| Creating `engine/middleware.py` | Don't | Chassis owns middleware |

## Code Quality
| ❌ Banned | ✅ Instead | Why |
|-----------|-----------|-----|
| `except:` (bare) | `except SpecificError as e:` | Silent failures |
| `except Exception: pass` | `except Exception: logger.error(...); raise` | Swallowed errors |
| `return None` from validators | `raise ValueError(...)` | Silent validation bypass |
| `# TODO` / `# FIXME` / `pass` in non-abstract | Implement or raise `NotImplementedError` | Dead code / stubs |
| `from typing import Any` (when type is known) | Use specific type | Type safety |

## Naming
| ❌ Banned | ✅ Instead | Why |
|-----------|-----------|-----|
| `matchentities` (flatcase) | `match_entities` (snake_case) | FIELD_NAMES.md contract |
| `nullBehavior` (camelCase) | `null_behavior` (snake_case) | FIELD_NAMES.md contract |
| `Field(alias="...")` | Use snake_case directly | PYDANTIC_YAML_MAPPING.md |
| `candidateprop` | `field` (per GateSpec) | FIELD_NAMES.md contract |
```

## Infrastructure (CONTRACT-05)

The engine never authors infrastructure. Dockerfiles, `docker-compose.yml`, CI pipelines,
Terraform modules, and k8s manifests all live in `l9-template`. The only infrastructure
surface the engine owns is adding engine-specific variables to `.env.template`
(see [ENV_VARS.md](ENV_VARS.md)).

| ❌ Banned in this repo | ✅ Instead |
|---|---|
| Creating a new `Dockerfile` / `docker-compose.yml` | Use the template's |
| Adding a `.github/workflows/*.yml` build pipeline | Use the template's |
| Terraform / Helm / k8s manifests | `l9-iac` template |

## File Structure (CONTRACT-16)

The `engine/` layout is fixed. Each concern has exactly one home:

| Path | Owns |
|---|---|
| `engine/handlers.py` | The **only** chassis bridge (`register_all`) |
| `engine/config/` | Domain spec schema, loader, settings, units |
| `engine/gates/` | Gate compiler, null semantics, registry, `types/` |
| `engine/scoring/` | Scoring assembler |
| `engine/traversal/` | Traversal assembler and resolver |
| `engine/sync/` | Sync generator |
| `engine/gds/` | GDS scheduler |
| `engine/graph/` | Neo4j async driver wrapper |
| `engine/compliance/` | Prohibited factors, PII, audit |
| `engine/packet/` | PacketEnvelope bridge |
| `engine/utils/` | `safe_eval`, `sanitize_label` |

Do not create new top-level directories under `engine/` without architectural approval.
`engine/api/` must not exist — it is a post-refactor ghost.

## Zero-Stub Protocol (CONTRACT-17)

`engine/` ships working code. An unimplemented path is an untestable path, so the stub
markers below are banned there and enforced by `tools/contract_scanner.py`.

| Rule | Pattern | Severity |
|---|---|---|
| `STUB-001` | `raise NotImplementedError` | CRITICAL |
| `STUB-002` | `# TODO` | HIGH |
| `STUB-003` | `# PLACEHOLDER`, `# FIXME`, `# XXX` | HIGH |

When you cannot finish the work now, record it in `DEFERRED.md` and remove the marker.
A tracked deferral is reviewable; an inline comment is not.

```python
# 🚫 BANNED — the gap is invisible outside this file
def compile_traversal_gate(spec: GateSpec) -> str:
    raise NotImplementedError  # TODO: needs multi-hop support
```

```python
# ✅ CORRECT — fail loudly at compile time, and log the gap in DEFERRED.md
def compile_traversal_gate(spec: GateSpec) -> str:
    if spec.hops > 1:
        msg = f"Multi-hop traversal gates are not supported (hops={spec.hops})"
        raise ValueError(msg)
    ...
```

**Scope note:** these rules apply to `engine/` only. Abstract base classes in `chassis/`
(for example `AuditSink.write_batch`) raise `NotImplementedError` as their defining
contract — that is the intended use, not a stub.
