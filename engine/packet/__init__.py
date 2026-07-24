"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [config]
tags: [platform, packet]
status: active
--- /L9_META ---

engine/packet — PacketEnvelope immutable communication protocol.
"""

from engine.packet.chassis_contract import deflate_egress, inflate_ingress
from engine.packet.packet_envelope import PacketEnvelope

__all__ = ["PacketEnvelope", "deflate_egress", "inflate_ingress"]
