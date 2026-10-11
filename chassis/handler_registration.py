"""
--- L9_META ---
l9_schema: 2
origin: chassis
engine: graph
layer: [api]
tags: [platform, chassis]
status: active
--- /L9_META ---

chassis/handler_registration.py
Registers engine.handlers.ACTION_HANDLERS with the constellation-node-sdk
handler registry, wrapping each handler with the PacketEnvelope audit side
effect that chassis/actions.py performs on the legacy path.

The audited request is the ingress TransportPacket the SDK hands the handler,
never a reconstruction: Gate owns its packet_id, trace_id, correlation_id and
lineage root, and the audited response derives from it (CEG-FIX-002).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from constellation_node_sdk import register_handler

from engine.handlers import ACTION_HANDLERS
from engine.packet.chassis_contract import deflate_egress
from engine.packet.packet_store import get_packet_store

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from constellation_node_sdk import TransportPacket

logger = logging.getLogger(__name__)

_ENGINE_VERSION = "1.1.0"
_RESPONDING_NODE = "graph-engine"


def _with_packet_audit(
    action: str,
    fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[str, dict[str, Any], TransportPacket], Awaitable[dict[str, Any]]]:
    """Wrap an engine handler so each call emits a request/response packet pair.

    The wrapper takes the SDK's packet-aware ``(tenant, payload, packet)`` form
    so the ingress packet itself is audited. Its parameters stay explicit: the
    SDK's ``_invoke_handler`` dispatches on
    ``len(inspect.signature(handler).parameters)``, so ``*args`` would silently
    reroute the call to the one-arg (packet) form. The engine handler keeps its
    ``(tenant, payload)`` contract.
    """

    async def wrapped(tenant: str, payload: dict[str, Any], packet: TransportPacket) -> dict[str, Any]:
        start = time.time()
        request_packet = packet

        try:
            engine_data = await fn(tenant, payload)
            status = "success"
        except Exception:
            # Re-raise so the SDK builds a failure TransportPacket, but record
            # the failed pair first so the audit trail is not lossy.
            response_packet = deflate_egress(
                request=request_packet,
                engine_data={"error": "handler_failed"},
                status="failed",
                processing_ms=(time.time() - start) * 1000,
                engine_version=_ENGINE_VERSION,
                responding_node=_RESPONDING_NODE,
            )
            await _persist(request_packet, response_packet)
            raise

        response_packet = deflate_egress(
            request=request_packet,
            engine_data=engine_data,
            status=status,
            processing_ms=(time.time() - start) * 1000,
            engine_version=_ENGINE_VERSION,
            responding_node=_RESPONDING_NODE,
        )
        await _persist(request_packet, response_packet)
        return engine_data

    wrapped.__name__ = f"{action}_with_packet_audit"
    return wrapped


async def _persist(request: Any, response: Any) -> None:
    """Persist a packet pair; store failures are warnings, never fatal."""
    try:
        await get_packet_store().persist(request, response)
    except Exception as store_exc:
        logger.warning("PacketStore.persist failed (non-fatal): %s", store_exc)


def register_engine_handlers() -> None:
    """Register every action in ACTION_HANDLERS with the SDK registry."""
    for action, handler in ACTION_HANDLERS.items():
        register_handler(action, _with_packet_audit(action, handler))
    logger.info(
        "SDK handler registry populated with %d actions: %s",
        len(ACTION_HANDLERS),
        ", ".join(ACTION_HANDLERS),
    )


__all__ = ["register_engine_handlers"]
