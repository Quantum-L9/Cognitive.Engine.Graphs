"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [integration]
tags: [gate, registration, sdk, startup]
owner: engine-team
status: active
--- /L9_META ---

engine/gate_registration.py — Gate self-registration hook for Cognitive.Engine.Graphs

Called from GraphLifecycle.startup() after init_dependencies().
Registration failure is intentionally non-fatal — the node starts regardless.
Gate does NOT discover unregistered nodes: its health monitor only probes nodes
already in its (in-memory) registry. GraphLifecycle therefore re-runs
registration on an interval (reregister_with_gate_forever) so the node becomes
routable again after a failed first attempt or a Gate restart.

Boot patch — add inside GraphLifecycle.startup() after init_dependencies():

    from engine.gate_registration import register_node_with_gate
    await register_node_with_gate()
"""

from __future__ import annotations

import asyncio
import logging

from constellation_node_sdk import register_from_env

logger = logging.getLogger(__name__)


async def register_node_with_gate() -> None:
    """Attempt Gate self-registration from environment config.

    On success: Gate routing table updated immediately.
    On failure: logged as warning; retried by reregister_with_gate_forever.
    Never raises — safe to call from inside lifecycle startup.
    """
    try:
        success = await register_from_env()
        if success:
            logger.info("gate_registration: node registered with Gate successfully")
        else:
            logger.warning(
                "gate_registration: registration returned False — "
                "node is unroutable until a re-registration succeeds. "
                "Check GATE_URL, GATE_ADMIN_TOKEN, and engine/spec.yaml."
            )
    except Exception as exc:
        logger.warning(
            "gate_registration: unexpected error during registration (non-fatal): %s",
            exc,
        )


async def reregister_with_gate_forever(interval_seconds: float) -> None:
    """Re-run Gate registration every ``interval_seconds`` until cancelled.

    register_node_with_gate never raises, so one failed cycle cannot end the
    loop — the loop exists to recover from exactly those failures.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await register_node_with_gate()
