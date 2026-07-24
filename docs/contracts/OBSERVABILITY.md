<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts]
owner: platform
status: active
/L9_META -->

**Closes:** Agents creating custom loggers, random metric names

```markdown
<!-- L9_TEMPLATE: true -->
# L9 Observability Contract

## Rule
All logging, metrics, and tracing are handled by the chassis. Engines NEVER
configure logging, create Prometheus counters, or manage trace context.

## Structured Log Fields (automatic via chassis)
Every log line emitted by the chassis includes:
```json
{
  "timestamp": "2026-03-01T20:00:00Z",
  "level": "info",
  "event": "request_processed",
  "trace_id": "abc-123-def",
  "tenant": "plasticos",
  "action": "match",
  "node": "graph-engine",
  "execution_ms": 45.2
}
```


## Engine Logging Pattern

The enforced invariant is **the engine never configures logging** — not which getter you call.
Both APIs below satisfy CONTRACT-04.

`logging.getLogger(__name__)` is the prevailing pattern in `engine/` (~80 modules).
`structlog.get_logger(__name__)` is used in a handful of newer modules (`engine/intake/`,
`engine/causal/serializer.py`, `engine/feedback/drift_detector.py`, `engine/security/`) and is
equally acceptable. Match the surrounding module; do not convert existing files.

```python
import logging
logger = logging.getLogger(__name__)

# ✅ CORRECT — stdlib logger, chassis owns handlers and formatting
logger.info("Gate compilation complete", extra={
    "gate_count": 10,
    "match_direction": "buyer_to_seller",
})

# ✅ ALSO CORRECT — structlog getter, still zero configuration
import structlog
logger = structlog.get_logger(__name__)
logger.info("gate_compilation_complete", gate_count=10)

# ❌ WRONG — configuring logging in engine
structlog.configure(...)                    # BANNED (OBS-001) — chassis does this

# ❌ WRONG — creating custom formatters
logging.basicConfig(format="...")           # BANNED (OBS-002) — chassis does this
```

> **Note:** no `structlog.configure()` call exists in `chassis/` in this repo either. Until the
> chassis adds one, `structlog` emits through stdlib logging defaults. That is a chassis gap,
> not an engine one — do not fix it from `engine/`.


## Prometheus Metrics (chassis-owned)

These metrics are auto-exported. Engines MUST NOT create their own.


| Metric | Type | Labels |
| :-- | :-- | :-- |
| `l9_request_total` | Counter | `action`, `tenant`, `status` |
| `l9_request_duration_seconds` | Histogram | `action`, `tenant` |
| `l9_request_errors_total` | Counter | `action`, `tenant`, `error_type` |

## Engine Custom Metrics (if absolutely needed)

> **Status: not available in this repo.** There is no `chassis/metrics.py` and no
> Prometheus registry in `chassis/`. The engine currently emits no custom metrics.
>
> If a custom metric becomes necessary, the chassis must first grow a metric factory —
> the engine still never creates raw Prometheus objects. The intended shape is:
>
> ```python
> gate_compilation_time = register_histogram(
>     "l9_gate_compilation_seconds",   # MUST start with l9_
>     "Time to compile all gates",
>     labels=["tenant", "match_direction"],
> )
> ```
>
> Adding that factory is a chassis change, not an engine change (`CONTRACT-04`).


## Trace Propagation

- `trace_id` is set by the chassis on inbound request
- Passed to engine via `payload` or context
- Included in all delegated packets automatically
- Engines NEVER generate their own trace IDs

```
