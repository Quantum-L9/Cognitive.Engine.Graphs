<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts, packet]
owner: platform
status: active
/L9_META -->

**Closes:** Agents inventing random packet type strings

```markdown
<!-- L9_TEMPLATE: true -->
# L9 Packet Type Registry

## Rule
`header.packet_type` on the wire is the closed set that the Gate_SDK
`constellation_node_sdk.transport.models.TransportHeader` validator enforces. CEG does
not own this vocabulary and must not extend it: a value outside the set raises
`ValueError` when the `TransportPacket` is constructed. Values are lowercase; the SDK
lowercases its input, and contract scanner rule `PKT-001` rejects an uppercase
`packet_type` literal in source.

## Current Registry (SDK closed set)

| packet_type | Built in CEG by | Meaning |
|------------|-----------------|---------|
| `request` | `inflate_ingress()` (`engine/packet/chassis_contract.py`), `build_request_packet()` (`engine/packet_bridge.py`) via SDK `create_transport_packet()` | Root packet of an exchange |
| `response` | `deflate_egress()`, `build_response_packet()` via `request.derive(packet_type="response")` | Result of a request; `lineage.parent_id` is the request |
| `event` | `PacketBridge.decision_packet()` / `outcome_packet()` (`engine/packet/bridge.py`) | Observational record derived from a packet |
| `delegation` | `delegate_to_node()` (`engine/packet/chassis_contract.py`) | Work handed to another node, with a `DelegationLink` |
| `failure` | Not built by CEG; read in `engine/gate_egress.py` (`response.header.packet_type == "failure"`) | Gate-side failure reply |
| `command` | Not built by CEG | SDK-defined |
| `replay_request` | Not built by CEG | SDK-defined |
| `replay_response` | Not built by CEG | SDK-defined |
| `compensation` | Not built by CEG | SDK-defined |

What a packet *does* is `header.action`, not `packet_type`: a lowercase string
matching `^[a-z0-9][a-z0-9._-]{0,63}$` (SDK `TransportHeader.validate_action`). Chassis
ingress actions are the registered handlers (`match`, `sync`, `admin`, `outcomes`,
`resolve`, `health`, `healthcheck`, `health-assess`, `enrich`, `score`); `PacketBridge` emits
`event` packets with actions `routing_decision` and `outcome_event`.

## WRONG (agents generate these)
```python
packet_type="REQUEST"            # WRONG -> "request" (lowercase; PKT-001)
packet_type="graph_match"        # WRONG -> packet_type="response"; the kind of work is header.action
packet_type="enrichment_result"  # WRONG -> not in the SDK set; rejected at construction
packet_type="routing_decision"   # WRONG -> packet_type="event", action="routing_decision"
```

## Retired names
Earlier revisions of this file listed 27 CEG-local names (`api_request`,
`graph_match`, `enrichment_result`, `routing_decision`, ...) as packet types. None of
them is a valid `header.packet_type`, and none is used as one in `engine/` or
`chassis/`. Only `routing_decision` occurs in code, as a `header.action`
(`engine/packet/bridge.py`). Do not reintroduce them as packet types.

## Adding a New Packet Type
CEG cannot. A new wire packet type is a Gate_SDK change to `TransportHeader`; CEG picks
it up by bumping `constellation-node-sdk`, then updates the table above and the
`TransportHeader.packet_type` enum in `docs/contracts/events/asyncapi.yaml`
(`tests/contracts/test_asyncapi_contract.py` fails until both match the SDK).
A new kind of work is a new `header.action`, which needs a handler, not a packet type.
```
