"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [packet]
tags: [bridge]
owner: engine-team
status: active
--- /L9_META ---
"""

from __future__ import annotations

from typing import Any

from constellation_node_sdk import TransportPacket, create_transport_packet

_ALLOWED_PACKET_TYPES = {
    "request",
    "response",
    "event",
    "command",
    "delegation",
    "failure",
    "replay_request",
    "replay_response",
    "compensation",
}


class PacketBridge:
    def inflate_ingress(
        self, *, tenant_id: str, actor: str, packet_type: str, payload: dict[str, Any]
    ) -> TransportPacket:
        header_type = packet_type if packet_type in _ALLOWED_PACKET_TYPES else "request"
        packet = create_transport_packet(
            action=packet_type,
            payload=payload,
            tenant={
                "actor": actor,
                "on_behalf_of": actor,
                "originator": actor,
                "org_id": tenant_id,
                "user_id": None,
            },
            source_node="graph",
            destination_node="graph",
            reply_to="graph",
        )
        if header_type != packet.header.packet_type:
            return packet.derive(packet_type=header_type)
        return packet

    def attach_entity_semantics(
        self,
        *,
        packet: TransportPacket,
        entity_type: str,
        canonical_entity_type: str,
    ) -> TransportPacket:
        payload = dict(packet.payload)
        payload["entity_type"] = entity_type
        payload["canonical_entity_type"] = canonical_entity_type
        return packet.derive(payload=payload)

    def decision_packet(self, *, packet: TransportPacket, decision: dict[str, Any]) -> TransportPacket:
        return packet.derive(packet_type="event", action="routing_decision", payload=decision)

    def outcome_packet(self, *, packet: TransportPacket, outcome: dict[str, Any]) -> TransportPacket:
        return packet.derive(packet_type="event", action="outcome_event", payload=outcome)
