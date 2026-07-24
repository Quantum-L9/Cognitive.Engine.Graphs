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
# L9 Dependency Injection Contract

## Rule
Engine dependencies (GraphDriver, DomainPackLoader, Redis) are injected
ONCE at startup via `init_dependencies()`. Handlers access them via module-level
references. No singletons, no service locators, no FastAPI Depends.

## Pattern (Current)
```python
# engine/handlers.py
_graph_driver: GraphDriver | None = None
_domain_loader: DomainPackLoader | None = None

def init_dependencies(graph_driver: GraphDriver, domain_loader: DomainPackLoader) -> None:
    global _graph_driver, _domain_loader
    _graph_driver = graph_driver
    _domain_loader = domain_loader
```


## Chassis Startup Sequence

```python
# chassis/app.py (or equivalent startup hook)
from engine.handlers import init_dependencies, register_all

async def lifespan(app):
    driver = GraphDriver(uri=settings.NEO4J_URI, ...)
    loader = DomainPackLoader(domains_dir=settings.DOMAINS_DIR)
    init_dependencies(driver, loader)
    register_all(chassis_router)
    yield
    await driver.close()
```


## BANNED Patterns

```python
# ❌ No creating drivers inside handlers
async def handle_match(tenant, payload):
    driver = GraphDriver(...)  # BANNED — creates new connection per request

# ❌ No importing settings directly in engine modules
from chassis.settings import get_settings  # BANNED — chassis concern

# ❌ No FastAPI Depends in engine code
from fastapi import Depends  # BANNED — chassis concern
```

```

## Resilience Patterns (CONTRACT-24)

Injected dependencies carry the resilience, so handlers do not have to.

**All Neo4j access goes through `GraphDriver.execute_query()`.** Raw `.session()` calls
outside `engine/graph/driver.py` and `engine/graph/circuit_breaker.py` are banned — they
bypass the circuit breaker.

The breaker is configured, not hardcoded: `neo4j_circuit_threshold`,
`neo4j_circuit_cooldown`, and `neo4j_circuit_half_open_max` in
`engine/config/settings.py`.

**Caches are bounded.** `domain_cache_maxsize` bounds the domain pack cache; any new cache
uses `cachetools.TTLCache` or an equivalent with an explicit ceiling. An unbounded dict
used as a cache is a memory leak with a slow fuse.

```python
# ❌ bypasses the circuit breaker
async with driver.session() as session: ...

# ❌ unbounded
_cache: dict[str, DomainSpec] = {}

# ✅
result = await graph_driver.execute_query(cypher, params)
_cache: TTLCache = TTLCache(maxsize=settings.domain_cache_maxsize, ttl=settings.domain_cache_ttl)
```
