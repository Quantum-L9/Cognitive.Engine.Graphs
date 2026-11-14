<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts]
owner: platform
status: active
/L9_META -->

**Closes:** Agents not knowing what the 26 inter-service feedback loops actually carry

## Status in this repository (2026-11-14)

The 26 loops below are the **target design** for the Constellation. Most of
them are not built. Read this section before implementing, citing or testing
any loop.

**Nodes.** Three of the eight services in the tables have a repository:

| Service | Owner | Notes |
|---|---|---|
| GRAPH | CEG (this repository) | ingress actions `match`, `sync`, `admin`, `outcomes`, `resolve`, `health`, `healthcheck`, `health-assess`, `enrich`, `score` (`engine/handlers.py` `ACTION_HANDLERS`) |
| SCORE | CEG `score` action, not a separate node | `handle_score_remote`; payload keys `entity_refs`, `top_n` only (`engine/scoring/evidence_priority.py` `REQUEST_KEYS`); refused with `FeatureNotEnabled` while `remote_score_enabled` is off, which it is everywhere (`docs/FEATURE_GATES.md` §29) |
| HEALTH | split | readiness scoring is CEG `health-assess`; the stale-entity enrichment trigger (loop 17) belongs to EIE (operator decision 2026-11-14) |
| ENRICH | EIE (`Quantum-L9/Enrichment.Inference.Engine`) | sender behaviour of loops 1–4 is not verified from this repository |
| ROUTE, FORECAST, SIGNAL, HANDOFF | none | no node exists; every loop to or from them is unbuilt |

**Routing.** Every loop travels through Gate. No node calls another node or
knows a peer URL; "To" names the destination Gate resolves, not an address.

**Kind column.** The values in the `packet_type` column are retired as wire
packet types (`docs/contracts/PACKET_TYPE_REGISTRY.md`, "Retired names"). On
the wire a loop is a `request` (or an `event`) whose `header.action` is the
Action column. Do not construct a `TransportPacket` with these strings as
`packet_type`.

**Loops whose destination action exists in CEG today:**

| # | Destination action | Gap |
|---|---|---|
| 1 | `score` | payload differs: CEG accepts `entity_refs`, `top_n`, not `entity_id`, `enriched_fields`, `confidence`; off by flag |
| 2 | `sync` | none on the CEG side |
| 8, 20 | `outcomes` (not `sync_outcome`) | action name differs; the outcome edge type is the domain's declared `feedbackloop.outcome_edge_type` |

Loops 13, 18 and 24 name `update_score` / `gate_confidence`, which CEG does not
register; their real destination would be `score`, whose request carries no
signal, health or affinity input. Graph affinity (loop 24) is computed inside
CEG `match` and never leaves the node. Loop 16 (`sync_signal`) has no handler.

```markdown
<!-- L9_TEMPLATE: true -->
# L9 Constellation Feedback Loop Contract

## Rule
Every feedback loop between services has a defined trigger, payload shape,
packet_type, and destination action. Agents implementing any service MUST
implement their inbound loops and fire their outbound loops.

## The 26 Loops

### ENRICH → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 1 | SCORE | Enrichment complete | `enrichment_result` | `score` | `entity_id`, `enriched_fields`, `confidence` |
| 2 | GRAPH | Enrichment complete | `graph_sync` | `sync` | `entity_type`, `batch` (enriched entities) |
| 3 | FORECAST | Enrichment complete | `enrichment_result` | `reforecast` | `entity_id`, `confidence`, `fields_filled` |
| 4 | HEALTH | Enrichment complete | `enrichment_result` | `update_health` | `entity_id`, `field_confidences` |

### SCORE → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 5 | ROUTE | Score computed | `score_record` | `route` | `entity_id`, `composite_score`, `dimension_scores` |
| 6 | FORECAST | Score computed | `score_record` | `update_feature` | `entity_id`, `score_velocity`, `dimension_scores` |
| 7 | ENRICH | Missing fields detected | `enrichment_request` | `enrich` | `entity_id`, `missing_fields`, `priority` |

### ROUTE → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 8 | GRAPH | Outcome recorded | `graph_sync` | `sync_outcome` | `entity_id`, `outcome`, `edge_type` = the domain's `feedbackloop.outcome_edge_type` declaration (example: PlasticOS `RESULTED_IN`) |
| 9 | FORECAST | Routing velocity | `routing_decision` | `update_feature` | `entity_id`, `routing_velocity`, `rep_id` |
| 10 | SIGNAL | Route event | `signal_event` | `ingest` | `entity_id`, `signal_type: "routed"`, `assigned_to` |

### FORECAST → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 11 | ENRICH | High-risk deal + missing fields | `enrichment_request` | `enrich` | `entity_id`, `missing_fields`, `risk_score` |
| 12 | ROUTE | Capacity rebalance needed | `routing_decision` | `rebalance` | `segment`, `capacity_recommendation` |

### SIGNAL → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 13 | SCORE | New signal | `signal_event` | `update_score` | `entity_id`, `engagement_score`, `intent_score` |
| 14 | FORECAST | Signal velocity change | `signal_event` | `update_feature` | `entity_id`, `signal_velocity`, `days_since_last` |
| 15 | ENRICH | New entity from signal | `enrichment_request` | `enrich` | `entity_id`, `entity_type`, `source` |
| 16 | GRAPH | Transaction signal | `graph_sync` | `sync_signal` | `entity_id`, `signal_type`, `edge_data` |

### HEALTH → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 17 | ENRICH | Stale/low-confidence entity | `enrichment_request` | `enrich` | `entity_id`, `stale_fields`, `health_score` |
| 18 | SCORE | Health gates confidence | `health_assessment` | `gate_confidence` | `entity_id`, `health_score` |
| 19 | FORECAST | AI readiness score | `health_assessment` | `gate_readiness` | `crm_health_score`, `field_health` |

### HANDOFF → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 20 | GRAPH | Transition outcome | `graph_sync` | `sync_outcome` | `deal_id`, `outcome`, `edge_type: "TRANSITIONED"` |
| 21 | ROUTE | Rep performance data | `routing_decision` | `update_rep_performance` | `rep_id`, `handoff_outcome` |
| 22 | SIGNAL | Transition event | `signal_event` | `ingest` | `deal_id`, `signal_type: "handoff"`, `transition_type` |
| 23 | FORECAST | Post-handoff update | `forecast_snapshot` | `update_deal` | `deal_id`, `outcome`, `new_owner` |

### GRAPH → (outbound)
| # | To | Trigger | Kind (retired as packet_type) | Action | Payload Keys |
|---|-----|---------|-------------|--------|-------------|
| 24 | SCORE | Graph affinity computed | `graph_match` | `update_score` | `entity_id`, `graph_affinity`, the domain's declared community property (example: PlasticOS `community_id`) |
| 25 | ROUTE | Match rankings | `graph_match` | `inform_routing` | `entity_id`, `match_rank`, `community_win_rate` |
| 26 | FORECAST | Community signals | `graph_match` | `update_feature` | `entity_id`, the domain's declared community property (example: PlasticOS `community_id`), `community_win_rate` |

## Implementation Rule
When building a service, check this table — and the status section above — for:
1. **Outbound loops**: your service MUST fire these packets on the listed triggers
2. **Inbound loops**: your service MUST handle these action types from other services
```
