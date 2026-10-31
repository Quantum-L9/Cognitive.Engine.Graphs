---
paths:
  - "engine/**/*.py"
---
# Subsystems & Handler Registry

## Action Handlers (engine/handlers.py)
All handlers: `async def handle_*(tenant: str, payload: dict) -> dict`

| Action | Handler | Purpose |
|--------|---------|---------|
| match | handle_match | Gate-then-score graph matching |
| sync | handle_sync | Batch entity ingestion (UNWIND MERGE) |
| admin | handle_admin | Domain management, GDS trigger, calibration |
| outcomes | handle_outcomes | Outcome feedback for score tuning |
| resolve | handle_resolve | Entity resolution / deduplication |
| health | handle_health | Field-level health assessment + readiness |
| healthcheck | handle_healthcheck | Alias for health |
| health-assess | handle_health_assess | Canonical Gate health route (HEALTH-01): only `health_assess` / `health_batch_assess` / `health_report`, never the rest of admin |
| enrich | handle_enrich | ROI-based re-enrichment triggering |
| score | handle_score_remote | Gate-routed cohort score (SCORING-GATE-01): default off, explicit tenant `score:read`, builds the evidence binding from the admitted spec, then calls the local `handle_score` |

## Admin Subactions (payload["subaction"])
| Subaction | Purpose |
|-----------|---------|
| list_domains | List loaded domain specs |
| get_domain | Return specific domain spec |
| init_schema | Initialize Neo4j schema |
| trigger_gds | Manual GDS algorithm run |
| calibration_run | Score calibration vs expected ranges |
| score_feedback | Compute weight adjustment proposal |
| apply_weight_proposal | Apply proposed weight change |
| health_assess | AI-readiness assessment for one entity (`engine/health/api.py`) |
| health_batch_assess | Incremental batch readiness scan with cost ceilings |
| health_report | Health report for an entity (Seed tier) |
| emit_inference_feedback | Run inference rules and send `graph-inference-result` to EIE via Gate (flag: `graph_inference_feedback_enabled`) |

CEG-006: the three `health_*` subactions are what makes `engine/health/api.py`
reachable. It was imported by nothing, so `trigger_reenrichment_v2` — and with
it the whole CEG → Gate → EIE enrichment request — had no trigger any inbound
packet could reach. HEALTH-01: Gate reaches them through the advertised
`health-assess` action (`handle_health_assess`), never through `admin`; the
admin subactions remain a direct operator surface. Both share `_dispatch_health`.

## Dependency Map
```
handlers.py (registers all 10 actions)
  ├── config/ → schema.py → settings.py
  ├── gates/compiler.py → gates/types/ → null_semantics.py
  ├── scoring/assembler.py → calibration.py, confidence.py, pareto.py
  ├── traversal/assembler.py → resolver.py
  ├── sync/generator.py
  ├── compliance/engine.py → pii.py, prohibited_factors.py
  ├── graph/driver.py (ALL Neo4j access — circuit breaker + timeout)
  ├── health/ → config, graph
  ├── intake/ → config, schema
  ├── personas/ (self-contained)
  ├── causal/ → schema, utils/security
  ├── feedback/ → schema, graph
  ├── resolution/ → schema, graph
  └── kge/ (dormant — kge_enabled=False)
boot.py (lifecycle — plus handlers.py, ONLY files importing chassis)
```

## Domain Spec Sections (engine/config/schema.py)
| Section | Schema Class | Purpose |
|---------|-------------|---------|
| ontology | OntologySpec | Node/edge types, properties |
| match_entities | MatchEntitiesSpec | Source/target definitions |
| query_schema | QuerySchemaSpec | Input parameters |
| traversal | TraversalSpec | Path patterns, hop limits |
| gates | list[GateSpec] | Hard filters (10 types) |
| scoring | ScoringSpec | Soft rank (13 computations) |
| sync | SyncSpec | Ingestion endpoints |
| gds_jobs | GDSSpec | Algorithm scheduling |
| kge | KGESpec | Embedding config |
| compliance | ComplianceSpec | PII, prohibited factors |
| calibration | CalibrationSpec | Expected score ranges |
| causal_edges | list[CausalEdgeSpec] | Causal relationships |
