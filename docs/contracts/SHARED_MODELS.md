<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts]
owner: platform
status: active
/L9_META -->

**Closes:** Agents duplicating Pydantic models across repos

```markdown
<!-- L9_TEMPLATE: true -->
# L9 Shared Models Contract

## Rule
Constellation-wide Pydantic models live in shared packages. Individual
engines import from these packages, NEVER redefine these models.

---

## Package: constellation-node-sdk (Gate SDK — Current Standard)

For inter-node communication, use the Gate SDK:

```python
# ✅ CORRECT — import from constellation_node_sdk
from constellation_node_sdk import TransportPacket, GateClient
from constellation_node_sdk import create_transport_packet
from constellation_node_sdk import get_gate_client_config_from_env
from constellation_node_sdk import register_from_env

# ✅ CORRECT — use engine wrappers
from engine.gate_client import get_gate_client
from engine.packet_bridge import build_request_packet, build_response_packet
```

Installation (pyproject.toml):
```toml
constellation-node-sdk = {git = "https://github.com/Quantum-L9/Gate_SDK.git", rev = "v1"}
```

`Quantum-L9/Gate_SDK` is canonical — `cryptoxdog/Gate_SDK` is a forbidden fork
and `scripts/validate_sdk_pin.py` fails closed on it. `v1` is the moving major
compatibility channel owned by Gate_SDK's `contracts/RELEASE_IDENTITY_LEDGER.json`;
declare that channel, never a commit sha and never a branch. The concrete object
it resolves to belongs in `uv.lock` (the commit in the `?tag=v1#<commit>` source).

---

## No internal envelope

There is no second, "internal" packet model. `PacketEnvelope` and the `l9_core` package
that declared it were deleted (Quantum-L9/Cognitive.Engine.Graphs#138);
`TransportPacket` is the only wire format inside the engine and between nodes.

## Import Pattern
```python
# ✅ CORRECT — the shared models come from constellation_node_sdk
from constellation_node_sdk import TenantContext, TransportLineage, TransportPacket

# ✅ CORRECT — engine code reaches packets only through the chassis bridge
from engine.packet.chassis_contract import deflate_egress, delegate_to_node, inflate_ingress


# ❌ WRONG — redefining TransportPacket
class TransportPacket(BaseModel):  # BANNED — already in constellation_node_sdk (SHARED-001)
    ...


# ❌ WRONG — reintroducing a parallel envelope
class PacketEnvelope(BaseModel):  # BANNED — retired; org Semgrep AST-TRANSPORT-001/002
    ...


# ❌ WRONG — copying the model file into your repo
# cp ../enrichment-engine/models/envelope.py engine/models/
```


## Engine-Specific Models Stay in Engine

```python
# These are engine-specific — they live in your engine repo
from engine.config.schema import DomainSpec, GateSpec, ScoringDimension
from engine.scoring.models import ScoreRecord
from engine.routing.models import RoutingDecision
```


## When to Add to the SDK vs Engine

| If the model is used by… | Where it lives |
| :-- | :-- |
| 2+ nodes | `constellation_node_sdk` (a Gate_SDK change) |
| 1 node only | That engine's `models/` |
| The chassis | `constellation_node_sdk` (chassis imports from it) |
| Tests only | `tests/conftest.py` fixtures |

```

## Tenant Isolation (CONTRACT-03)

`TenantContext` is a shared model — the engine never redefines it (`SHARED-002`) and never
resolves it. The chassis performs the 5-level resolution (header → subdomain → key prefix
→ envelope → default) and hands the engine a plain `tenant: str`.

Every Neo4j query scopes to that tenant's database. There is no cross-tenant read path:
not in matching, not in sync, not in the KGE vector index
(see [KGE_EMBEDDINGS.md](KGE_EMBEDDINGS.md)).

```python
# ✅ tenant arrives as an argument
async def handle_match(tenant: str, payload: dict[str, Any]) -> dict[str, Any]: ...

# ❌ engine resolving tenant itself
tenant = request.headers["X-Tenant-Id"]   # BANNED — chassis concern
class TenantContext(BaseModel): ...       # BANNED — SHARED-002
```
