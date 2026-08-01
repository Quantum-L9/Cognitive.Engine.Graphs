"""
--- L9_META ---
l9_schema: 2
origin: chassis
engine: graph
layer: [api]
tags: [platform, chassis]
status: active
--- /L9_META ---

chassis/node_app.py
SDK-native chassis: builds the FastAPI app via constellation-node-sdk
create_node_app, with GraphLifecycle adapted to the SDK LifecycleHook.

Selected by L9_CHASSIS=sdk (see chassis/entrypoint.py). The legacy
chassis/chassis_app.py remains the default until parity tests pass.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from constellation_node_sdk import LifecycleHook as SdkLifecycleHook
from constellation_node_sdk import create_node_app
from fastapi.responses import JSONResponse

from chassis.handler_registration import register_engine_handlers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI, Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

_EXECUTE_PATH = "/v1/execute"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _gate_ingress_violation(body: dict[str, Any], gate_node: str) -> str | None:
    """Return a rejection reason if the packet is not Gate-authored, else None.

    NodeRuntimeConfig has no gate-only ingress field, so this is enforced here
    rather than by the SDK. Checks mirror the Gate's own dispatch-authority
    rules: origin_kind == "gate", resolved_by_gate, and source_node == gate.
    """
    provenance = body.get("provenance")
    address = body.get("address")
    if not isinstance(provenance, dict) or not isinstance(address, dict):
        return "packet must carry provenance and address"

    origin_kind = str(provenance.get("origin_kind", "")).strip().lower()
    if origin_kind != "gate":
        return f"provenance.origin_kind must be 'gate', got {origin_kind!r}"

    if not provenance.get("resolved_by_gate", False):
        return "provenance.resolved_by_gate must be true"

    source_node = str(address.get("source_node", "")).strip().lower()
    if source_node != gate_node:
        return f"address.source_node must be {gate_node!r}, got {source_node!r}"

    return None


def _install_gate_only_ingress(app: FastAPI, *, gate_node: str) -> None:
    """Reject non-Gate-authored packets on /v1/execute before the SDK sees them."""

    @app.middleware("http")
    async def gate_only_ingress(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method != "POST" or request.url.path != _EXECUTE_PATH:
            return await call_next(request)

        raw = await request.body()

        # BaseHTTPMiddleware consumes the request stream; replay it so the
        # SDK route can still parse the body.
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw, "more_body": False}

        # Documented Starlette workaround: BaseHTTPMiddleware has no public
        # API for replaying a consumed request stream.
        request._receive = receive

        try:
            body = json.loads(raw)
        except ValueError:
            # Malformed JSON is the SDK route's 400 to raise, not ours.
            return await call_next(request)

        if not isinstance(body, dict):
            return await call_next(request)

        reason = _gate_ingress_violation(body, gate_node)
        if reason is not None:
            logger.warning("Gate-only ingress rejected packet: %s", reason)
            return JSONResponse(
                status_code=403,
                content={"detail": f"gate-only ingress: {reason}"},
            )

        return await call_next(request)


class SdkLifecycleAdapter(SdkLifecycleHook):
    """Adapt engine.boot.GraphLifecycle onto the SDK LifecycleHook ABC.

    GraphLifecycle subclasses the *legacy* chassis LifecycleHook. Both
    interfaces are startup/shutdown only, but they are unrelated classes,
    so the SDK requires an explicit adapter. Keeping the adapter here
    leaves engine/boot.py byte-identical during dual-run.
    """

    def __init__(self) -> None:
        from engine.boot import GraphLifecycle

        self._inner = GraphLifecycle()

    async def startup(self) -> None:
        await self._inner.startup()

    async def shutdown(self) -> None:
        await self._inner.shutdown()


def create_app() -> FastAPI:
    """Build the SDK-native node app with engine handlers registered.

    auto_register_with_gate=False: GraphLifecycle.startup() already calls
    register_node_with_gate(), so letting the SDK lifespan also call
    register_from_env() would double-register whenever GATE_URL is set.
    """
    register_engine_handlers()
    logger.info("Building SDK chassis app (L9_CHASSIS=sdk)")
    app = create_node_app(
        lifecycle_hook=SdkLifecycleAdapter(),
        auto_register_with_gate=False,
    )

    if _env_bool("L9_ENFORCE_GATE_ONLY_INGRESS", default=True):
        gate_node = os.environ.get("L9_GATE_NODE_NAME", "gate").strip().lower()
        _install_gate_only_ingress(app, gate_node=gate_node)
        logger.info("Gate-only ingress enforced (gate node: %s)", gate_node)
    else:
        logger.warning("Gate-only ingress DISABLED - /v1/execute accepts any packet origin")

    return app


__all__ = ["SdkLifecycleAdapter", "create_app"]
