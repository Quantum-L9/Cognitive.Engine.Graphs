<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [packet, envelope]
owner: engine-team
status: active
/L9_META -->

# engine/packet — TransportPacket bridge and packet store

The filename is historical. CEG has no packet model of its own: `TransportPacket` from
`constellation_node_sdk` is the only wire format. The former `PacketEnvelope`
declaration (`packet_envelope.py`) and its `l9_core` twin were deleted under
Quantum-L9/Cognitive.Engine.Graphs#138; nothing at runtime used them.

## The files

| File | Purpose |
| :-- | :-- |
| `chassis_contract.py` | `inflate_ingress` (client JSON → root `TransportPacket`), `deflate_egress` (derived response + response hop), `delegate_to_node` (derived delegation packet + delegation link + dispatch hop) |
| `bridge.py` | `PacketBridge` adapter over the SDK packet |
| `packet_store.py` | Persists request/response packet pairs; `content_hash` column = `security.transport_hash` (UNIQUE) |
| `packet_store.sql` | DDL with row-level security, audit log, GDPR erasure, lineage and hop-trace tables |

Field names, the hash formula and the immutability rules are the contract in
`docs/contracts/PACKET_ENVELOPE_FIELDS.md`; tests are in `tests/unit/test_packet_envelope.py`
and `tests/unit/test_chassis_contract.py`.

## Rules

- Packets are immutable. A change is a new packet: `derive()` for new content or
  routing, `with_hop()` to append a hop. `hop_trace` is outside `transport_hash`, so a
  hop never invalidates the signed core.
- Lineage is `parent_id` / `root_id` / `generation`, set by `derive()`.
- The tenant never changes across `derive()`.
- Delegation always goes through `delegate_to_node()`, which sets `audit_required=True`.
