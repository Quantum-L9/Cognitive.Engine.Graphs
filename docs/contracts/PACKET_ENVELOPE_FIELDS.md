<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts, packet]
owner: platform
status: active
/L9_META -->

<!-- L9_TEMPLATE: true -->
# L9 TransportPacket Field Contract

> The filename is historical. `PacketEnvelope` is retired: both declarations
> (`engine/packet/packet_envelope.py`, `l9_core/models.py`) were deleted under
> Quantum-L9/Cognitive.Engine.Graphs#138. `TransportPacket` from
> `constellation_node_sdk` is the **only** wire format, inside the engine and between
> nodes. Constructing or deriving anything else is forbidden (org Semgrep
> `AST-TRANSPORT-001`–`004`, baseline ratchet `pre-commit/packet-envelope-prohibited`).

## Rule

`TransportPacket` and every sub-model are `frozen=True, extra="forbid"`. Every node that
creates, reads, or derives a packet uses these exact field names. No aliases, no
abbreviations, no local redefinition (`SHARED-001`).

A packet is created only by:

- `create_transport_packet(...)` — a root packet (the SDK factory);
- `packet.derive(...)` — a child with new lineage and recomputed hashes;
- `packet.with_hop(hop)` — the same packet with one more `hop_trace` entry.

In CEG those calls sit behind `engine/packet/chassis_contract.py`
(`inflate_ingress`, `deflate_egress`, `delegate_to_node`) and `engine/packet_bridge.py`
(`build_request_packet`, `build_response_packet`). Handler code calls those, not the SDK.

```python
from engine.packet.chassis_contract import deflate_egress, delegate_to_node, inflate_ingress
from engine.packet_bridge import build_request_packet, build_response_packet

packet = build_request_packet(
    action="graph-query",
    payload={"cypher": "MATCH (n) RETURN n"},
    tenant="plasticos",
    trace_id="trace-12345",
)
packet.header.action  # "graph-query"
packet.header.trace_id  # "trace-12345"
packet.payload  # {"cypher": "..."}
packet.tenant.actor  # "plasticos"
```

## Top-level fields

```python
class TransportPacket(BaseModel):  # frozen, extra=forbid — constellation_node_sdk
    header: TransportHeader
    address: TransportAddress
    tenant: TenantContext
    payload: dict[str, Any]  # the only field that carries domain data
    security: TransportSecurity
    governance: TransportGovernance
    provenance: RoutingProvenance
    delegation_chain: tuple[DelegationLink, ...] = ()
    hop_trace: tuple[TransportHop, ...] = ()  # append-only, outside transport_hash
    lineage: TransportLineage
    attachments: tuple[TransportAttachment, ...] = ()
```

## Sub-models

### TransportHeader

```python
packet_id: UUID  # auto-generated, globally unique
packet_type: str  # closed set, see PACKET_TYPE_REGISTRY.md
action: str  # lowercase, ^[a-z0-9][a-z0-9._-]{0,63}$
priority: int = 2  # 0..3
created_at: datetime  # UTC
expires_at: datetime | None
timeout_ms: int = 30_000
schema_version: str = "1.0"
idempotency_key: str | None
trace_id: str | None  # W3C trace id, preserved across derive()
correlation_id: str | None  # preserved across derive()
causation_id: UUID | None  # parent packet_id on a derived packet
retry_count: int = 0
replay_mode: bool = False
not_before: datetime | None
```

### TransportAddress

```python
source_node: str
destination_node: str
reply_to: str
```

### TenantContext

```python
actor: str  # who is doing it
on_behalf_of: str  # who authorized it (defaults to actor)
originator: str  # who started the chain (defaults to actor)
org_id: str  # tenant isolation key
user_id: str | None  # individual actor
```

`derive()` never changes the tenant (`assert_tenant_immutable`).

### TransportSecurity

```python
payload_hash: str  # sha256 of canonical payload
transport_hash: str  # sha256 of the transport core (below)
signature: str | None
signature_algorithm: str | None  # "hmac-sha256" | "ed25519"
signing_key_id: str | None
classification: str = "internal"  # public | internal | confidential | restricted
encryption_status: str = "plaintext"
pii_fields: tuple[str, ...] = ()  # payload keys holding PII (GDPR ops)
```

### TransportGovernance

```python
intent: str
compliance_tags: tuple[str, ...] = ()
retention_days: int = 90
redaction_applied: bool = False
audit_required: bool = False  # delegate_to_node() sets True
data_subject_id: str | None
```

### TransportLineage

```python
parent_id: UUID | None  # None on a root packet
root_id: UUID  # the root packet's packet_id
generation: int = 0  # +1 per derive()
```

### DelegationLink

```python
delegator: str
delegatee: str
scope: tuple[str, ...]
granted_at: datetime
expires_at: datetime | None
constraints: dict[str, Any] | None
proof: str | None
```

`RoutingProvenance`, `TransportHop` and `TransportAttachment` are defined by the SDK
(`constellation_node_sdk.transport`); CEG does not construct them directly — hops come
from `make_dispatch_hop` / `make_response_hop`.

## Immutability Contract

```python
# ✅ CORRECT — derive creates a new packet
child = packet.derive(packet_type="response", payload=new_payload)
# child.lineage.parent_id == packet.header.packet_id
# child.lineage.generation == packet.lineage.generation + 1
# child.lineage.root_id == packet.lineage.root_id

# ✅ CORRECT — a hop is appended, the signed core is unchanged
traced = child.with_hop(hop)

# ❌ BANNED — direct mutation raises ValidationError ("Instance is frozen")
packet.payload = {"new_field": "value"}

# ❌ BANNED — an in-place edit of the payload dict is not blocked, but the packet
# then fails re-validation with TransportIntegrityError. Never do it.
packet.payload["new_field"] = "value"
```

## Hash Computation

This is the formula `constellation_node_sdk.transport.hashing` implements
(`compute_payload_hash`, `compute_transport_hash`); `tests/unit/test_packet_envelope.py`
checks this block against the SDK source. Canonical JSON is sorted keys, compact
separators, UTC datetimes and string UUIDs (`canonical_json`).

```python
import hashlib

from constellation_node_sdk.transport.hashing import canonical_json

payload_hash = hashlib.sha256(canonical_json(packet.payload)).hexdigest()
transport_hash = hashlib.sha256(
    canonical_json(
        {
            "header": packet.header,
            "address": packet.address,
            "tenant": packet.tenant,
            "payload": packet.payload,
            "governance": packet.governance,
            "delegation_chain": packet.delegation_chain,
            "lineage": packet.lineage,
            "attachments": packet.attachments,
            "provenance": packet.provenance,
            "payload_hash": packet.security.payload_hash,
        }
    )
).hexdigest()
```

`hop_trace` and the signature fields are outside `transport_hash`, which is why
`with_hop()` keeps an existing signature valid. `TransportPacket` re-checks both hashes
on every construction and validation, and raises `TransportIntegrityError` on a
mismatch, so a tampered or forged packet is rejected at deserialization.

`packet_store` persists `security.transport_hash` in its `content_hash` column
(UNIQUE; RU-078, `PACKET_STORE_PACKET_IDENTITY_DEDUP_ENABLED`; `security.payload_hash`
with that flag off) — see `docs/FEATURE_GATES.md` §7.

## WRONG Field Names (agents generate these — they're all wrong)

```python
packetid  # WRONG → header.packet_id
packet.packet_id  # WRONG → packet.header.packet_id
packettype  # WRONG → header.packet_type
contentHash  # WRONG → security.transport_hash (or security.payload_hash)
content_hash  # WRONG on a packet → security.transport_hash (content_hash is the packet_store column)
traceId  # WRONG → header.trace_id
parent_ids  # WRONG → lineage.parent_id (single parent)
sourceNode  # WRONG → address.source_node
onBehalfOf  # WRONG → tenant.on_behalf_of
```

## The Only Data Container (CONTRACT-06)

Every inter-service payload is a `TransportPacket`. The boundary functions are the only
sanctioned way in and out:

```python
from engine.packet.chassis_contract import deflate_egress, inflate_ingress
```

- `inflate_ingress()` at boundary **entry**
- `deflate_egress()` at boundary **exit**
- Engine code **between** boundaries works with typed dicts and Pydantic models, never
  raw packets

`packet_type` values are lowercase (`PKT-001`). Persistence goes through
`engine/packet/packet_store.py` — direct `INSERT INTO packetstore` or
`INSERT INTO memory_embeddings` from engine code is banned (`MEM-001`, `MEM-002`); see
[MEMORY_SUBSTRATE_ACCESS.md](MEMORY_SUBSTRATE_ACCESS.md).
