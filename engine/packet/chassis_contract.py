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

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from constellation_node_sdk import (
    DelegationLink,
    TransportGovernance,
    TransportPacket,
    create_transport_packet,
)
from constellation_node_sdk.transport.hop_trace import make_dispatch_hop, make_response_hop
from neo4j.time import Date as _NeoDate
from neo4j.time import DateTime as _NeoDateTime
from neo4j.time import Duration as _NeoDuration
from neo4j.time import Time as _NeoTime


class EgressValueError(ValueError):
    """A response value that has no JSON form (D1 / SC-003).

    Raised by ``deflate_egress`` for a non-finite float or an unserialisable
    object anywhere in ``engine_data``. ``path`` names where the value sits
    (``data.candidates[0].properties.rate_per_ton``); the value itself is never
    echoed (it may be PII).
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Response value at {path}: {reason}")


_NEO4J_TEMPORALS = (_NeoDateTime, _NeoDate, _NeoTime, _NeoDuration)
_JSON_SCALARS = (str, int, bool, type(None))


def _egress_scalar(value: Any, path: str) -> Any:
    """The JSON-safe form of one leaf: temporals as ISO-8601, other leaves unchanged or refused."""
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EgressValueError(path, "non-finite float has no JSON form")
        return value
    if isinstance(value, _NEO4J_TEMPORALS):
        return value.iso_format()
    if isinstance(value, datetime | date | time):  # datetime before date: both share isoformat
        return value.isoformat()
    if isinstance(value, timedelta):
        return _NeoDuration(days=value.days, seconds=value.seconds, microseconds=value.microseconds).iso_format()
    msg = f"{type(value).__name__} is not JSON-serialisable"
    raise EgressValueError(path, msg)


def _normalise(value: Any, path: str) -> Any:
    """Normalise ``value``; dicts and lists are rewritten in place and returned as the same object."""
    if isinstance(value, _NEO4J_TEMPORALS):  # first: neo4j.time.Duration is a tuple subclass
        return value.iso_format()
    if isinstance(value, dict):
        for key in list(value):
            value[key] = _normalise(value[key], f"{path}.{key}")
        return value
    if isinstance(value, list):
        for index, element in enumerate(value):
            value[index] = _normalise(element, f"{path}[{index}]")
        return value
    if isinstance(value, tuple):
        return tuple(_normalise(element, f"{path}[{index}]") for index, element in enumerate(value))
    return _egress_scalar(value, path)


def normalise_egress_values(engine_data: dict[str, Any], *, root: str = "data") -> dict[str, Any]:
    """Make ``engine_data`` JSON-safe once, in place, before it leaves the engine (D1 / ND-SERIAL).

    Every Neo4j or Python temporal becomes its ISO-8601 string (Neo4j values keep
    nanosecond precision and their offset); a non-finite float or any other
    value with no JSON form raises ``EgressValueError`` naming its path. In
    place, so the dict the chassis returns (``chassis/actions.py`` returns the
    handler's own dict as ``data``) and the response packet carry the same
    values. Returns ``engine_data``.
    """
    _normalise(engine_data, root)  # a dict is rewritten in place
    return engine_data


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

    With ``egress_value_normalisation_enabled`` (default on) ``engine_data`` is
    first normalised in place by ``normalise_egress_values``: temporals become
    ISO-8601, and a non-finite float raises ``EgressValueError``.
    """
    from engine.config.settings import settings

    if settings.egress_value_normalisation_enabled:
        normalise_egress_values(engine_data)
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
