"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api]
tags: [chassis, actions]
owner: platform-team
status: active
--- /L9_META ---

chassis/actions.py
Chassis integration: inflate requests, call engine handlers, deflate responses.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-loaded engine components (initialized on first use)
_engine_handlers: dict[str, Any] | None = None
_inflate_ingress: Any = None
_deflate_egress: Any = None


def _init_engine() -> None:
    """Lazy-initialize engine handlers and packet functions."""
    global _engine_handlers, _inflate_ingress, _deflate_egress

    if _engine_handlers is not None:
        return

    try:
        from engine.handlers import ACTION_HANDLERS
        from engine.packet.chassis_contract import deflate_egress, inflate_ingress

        _engine_handlers = dict(ACTION_HANDLERS)
        _inflate_ingress = inflate_ingress
        _deflate_egress = deflate_egress
        logger.info("Engine handlers initialized: %d actions registered", len(_engine_handlers))
    except ImportError as e:
        logger.exception(f"Failed to import engine handlers: {e}")
        _engine_handlers = {}
        raise RuntimeError("Engine initialization failed") from e


def _get_engine_error_class() -> type | None:
    """Get EngineError class if available (for error type checking)."""
    try:
        from engine.handlers import EngineError
    except ImportError:
        return None
    else:
        return EngineError


async def execute_action(
    action: str,
    payload: dict[str, Any],
    tenant: str,
    trace_id: str,
) -> dict[str, Any]:
    """
    Chassis entrypoint: POST /v1/execute

    1. Inflate inbound JSON → TransportPacket (request)
    2. Route to engine handler by action
    3. Execute handler
    4. Deflate engine response → TransportPacket (response)
    5. Return outbound envelope as JSON
    """
    # Lazy-initialize engine on first call
    _init_engine()

    start_time = time.time()

    # Inflate request into TransportPacket
    request_packet = _inflate_ingress(
        action=action,
        payload=payload,
        tenant=tenant,
        trace_id=trace_id,
        source_node="chassis",
    )

    # Route to engine handler
    handler = _engine_handlers.get(action) if _engine_handlers else None
    if not handler:
        raise ValueError(f"Unknown action: {action!r}")

    # Execute engine logic
    engine_error_cls = _get_engine_error_class()
    http_hint: dict[str, Any] | None = None
    try:
        engine_data = await handler(tenant, payload)
        status = "success"
    except Exception as e:
        # W4-02: Circuit breaker open → 503 Service Unavailable
        from engine.graph.circuit_breaker import CircuitOpenError

        if isinstance(e, CircuitOpenError):
            logger.warning("Circuit breaker OPEN for %s: %s", action, e)
            engine_data = {"error": str(e), "retry_after": e.retry_after, "status_code": 503}
            status = "circuit_open"
        elif engine_error_cls is not None and isinstance(e, engine_error_cls):
            logger.warning(f"Engine error in {action}: {e}")
            engine_data = {"error": str(e)}
            status = "failed"
        else:
            logger.exception(f"Handler {action} failed for tenant={tenant}")
            engine_data = {"error": str(e)}
            status = "failed"
        http_hint = _http_status_hint(e, engine_error_cls)

    # Deflate response into TransportPacket
    processing_ms = (time.time() - start_time) * 1000
    _response_packet = _deflate_egress(
        request=request_packet,
        engine_data=engine_data,
        status=status,
        processing_ms=processing_ms,
        engine_version="1.1.0",
        responding_node="graph-engine",
    )

    # Persist TransportPacket pair to audit trail (stub — no-op until DB wired)
    try:
        from engine.packet.packet_store import get_packet_store

        await get_packet_store().persist(request_packet, _response_packet)
    except Exception as store_exc:
        logger.warning("PacketStore.persist failed (non-fatal): %s", store_exc)

    # Return outbound envelope as JSON
    result: dict[str, Any] = {
        "status": status,
        "action": action,
        "tenant": tenant,
        "data": engine_data,
        "meta": {
            "trace_id": trace_id,
            "execution_ms": processing_ms,
            "version": "1.1.0",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }
    if http_hint is not None:
        # Outside "data" and not persisted: chassis_app pops these before it
        # builds the HTTP response (RU-083).
        result.update(http_hint)
    return result


# HTTP status hint keys (RU-083). Top-level, never inside "data".
HTTP_STATUS_KEY = "http_status"
HTTP_DETAIL_KEY = "http_detail"
HTTP_RETRY_AFTER_KEY = "http_retry_after"
_GENERIC_500_DETAIL = "Internal server error"


def _http_status_hint(exc: Exception, engine_error_cls: type | None) -> dict[str, Any] | None:
    """Map a handler exception to the HTTP status and client-safe detail (RU-083).

    CircuitOpenError -> 503 (+ retry_after); ChassisError -> its status_code;
    engine ValidationError -> 422; other EngineError -> 500 with its message;
    anything else -> 500 "Internal server error" (the exception text stays in
    the log and in the persisted response packet, never in the HTTP body).
    Returns None when LEGACY_CHASSIS_STATUS_MAPPING_ENABLED is off.
    """
    from engine.config.settings import settings

    if not settings.legacy_chassis_status_mapping_enabled:
        return None

    from chassis.errors import ChassisError
    from engine.graph.circuit_breaker import CircuitOpenError

    if isinstance(exc, CircuitOpenError):
        return {HTTP_STATUS_KEY: 503, HTTP_DETAIL_KEY: str(exc), HTTP_RETRY_AFTER_KEY: exc.retry_after}
    if isinstance(exc, ChassisError):
        return {HTTP_STATUS_KEY: exc.status_code, HTTP_DETAIL_KEY: str(exc)}
    if engine_error_cls is not None and isinstance(exc, engine_error_cls):
        from engine.handlers import ValidationError as EngineValidationError

        status = 422 if isinstance(exc, EngineValidationError) else 500
        return {HTTP_STATUS_KEY: status, HTTP_DETAIL_KEY: str(exc)}
    return {HTTP_STATUS_KEY: 500, HTTP_DETAIL_KEY: _GENERIC_500_DETAIL}
