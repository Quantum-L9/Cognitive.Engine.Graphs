"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [packet, chassis-bridge]
owner: engine-team
status: active
--- /L9_META ---
"""
# L9 Chassis ↔ TransportPacket bridge
# Inflates minimal client JSON → full constellation TransportPacket.
# Deflates engine response → wire-safe outbound envelope.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from constellation_node_sdk import (
    DelegationLink,
    TransportGovernance,
    TransportPacket,
    create_transport_packet,
)
from constellation_node_sdk.transport.hop_trace import make_dispatch_hop, make_response_hop


def _action_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).strip().lower()


def inflate_ingress(
    *,
    action: str,
    payload: dict[str, Any],
    tenant: str,
    trace_id: str,
    source_node: str = "chassis",
    intent: str | None = None,
    classification: str = "internal",
    on_behalf_of: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> TransportPacket:
    """
    Called by the chassis when a client POST /v1/execute arrives.
    Minimal input → full TransportPacket ready for engine consumption.
    """
    actor = tenant.strip()
    packet = create_transport_packet(
        action=action,
        payload=payload,
        tenant={
            "actor": actor,
            "on_behalf_of": (on_behalf_of or actor),
            "originator": actor,
            "org_id": (org_id or actor),
            "user_id": user_id,
        },
        source_node=source_node,
        destination_node="graph",
        reply_to=source_node,
        classification=classification,
        trace_id=trace_id,
    )
    if intent:
        packet = packet.derive(
            governance=TransportGovernance(
                intent=intent,
                compliance_tags=packet.governance.compliance_tags,
                retention_days=packet.governance.retention_days or 90,
                redaction_applied=bool(packet.governance.redaction_applied),
                audit_required=bool(packet.governance.audit_required),
                data_subject_id=packet.governance.data_subject_id,
            )
        )
    return packet


def deflate_egress(
    *,
    request: TransportPacket,
    engine_data: dict[str, Any],
    status: str = "success",
    processing_ms: float,
    engine_version: str = "0.0.0",
    responding_node: str,
) -> TransportPacket:
    """
    Called by the chassis after engine returns.
    Creates a response TransportPacket derived from the request.
    """
    now = datetime.now(UTC)
    destination = request.address.reply_to or request.address.source_node
    response = request.derive(
        packet_type="response",
        payload={
            "status": status,
            "data": engine_data,
            "meta": {
                "trace_id": request.header.trace_id,
                "execution_ms": processing_ms,
                "version": engine_version,
                "timestamp": now.isoformat(),
            },
        },
        source_node=responding_node,
        destination_node=destination,
        reply_to=responding_node,
    )
    hop_status = "completed" if status == "success" else "failed"
    hop = make_response_hop(
        packet=response,
        node=responding_node,
        action=request.header.action,
        status=hop_status,
        duration_ms=max(0, int(processing_ms)),
        error_message=None if status == "success" else status,
    )
    return response.with_hop(hop)


def delegate_to_node(
    *,
    source_packet: TransportPacket,
    from_node: str,
    to_node: str,
    delegated_action: Any,
    scope: tuple[str, ...],
    payload_override: dict[str, Any] | None = None,
) -> TransportPacket:
    """
    Called when one constellation node delegates work to another.
    Creates a DELEGATION packet with proper tenant context + auth chain.
    """
    now = datetime.now(UTC)
    action = _action_name(delegated_action)
    retention = source_packet.governance.retention_days
    child = source_packet.derive(
        packet_type="delegation",
        action=action,
        payload=payload_override if payload_override is not None else source_packet.payload,
        source_node=from_node,
        destination_node=to_node,
        reply_to=from_node,
        delegation_link=DelegationLink(
            delegator=from_node,
            delegatee=to_node,
            scope=scope,
            granted_at=now,
        ),
        governance=TransportGovernance(
            intent=f"Delegated {action} to {to_node}",
            compliance_tags=source_packet.governance.compliance_tags,
            retention_days=90 if retention is None else retention,
            redaction_applied=bool(source_packet.governance.redaction_applied),
            audit_required=True,
            data_subject_id=source_packet.governance.data_subject_id,
        ),
    )
    hop = make_dispatch_hop(
        packet=child,
        node=from_node,
        action="delegate",
        target_node=to_node,
        status="delegated",
    )
    return child.with_hop(hop)
