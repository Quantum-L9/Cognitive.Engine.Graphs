"""
--- L9_META ---
l9_schema: 2
origin: chassis
engine: graph
layer: [api]
tags: [platform, chassis]
status: active
--- /L9_META ---

chassis/entrypoint.py
Single uvicorn target for both chassis implementations.

    L9_CHASSIS=sdk     (default) -> chassis.node_app.create_app
    L9_CHASSIS=legacy            -> chassis.chassis_app.create_app  (dev/test only)

Every launch site (scripts/entrypoint.sh, Dockerfile.prod, Makefile) points
at chassis.entrypoint:create_app so switching chassis is a config change,
not a command change.

Seam audit 2026-09-02: the legacy chassis accepts a dict ExecuteRequest with
api-key auth and no Gate provenance — a direct-ingress side door around Gate.
It is therefore refused at startup outside dev/local/test environments while
``settings.require_sdk_chassis_in_prod`` is on (the default).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

LEGACY = "legacy"
SDK = "sdk"
DEFAULT_CHASSIS = SDK
_VALID = (LEGACY, SDK)
# Environments in which the legacy dict chassis may still be selected explicitly.
LEGACY_PERMITTED_ENVS: frozenset[str] = frozenset({"dev", "local", "test"})


def resolve_chassis() -> str:
    """Return the selected chassis name, validating L9_CHASSIS."""
    selected = os.environ.get("L9_CHASSIS", DEFAULT_CHASSIS).strip().lower()
    if selected not in _VALID:
        msg = f"L9_CHASSIS must be one of {_VALID}, got {selected!r}"
        raise ValueError(msg)

    from engine.config.settings import settings

    if selected != SDK and settings.require_sdk_chassis_in_prod and settings.l9_env not in LEGACY_PERMITTED_ENVS:
        msg = (
            f"L9_CHASSIS must be {SDK!r} outside {sorted(LEGACY_PERMITTED_ENVS)} "
            f"(l9_env={settings.l9_env!r}, production={settings.is_production}); got {selected!r}"
        )
        raise ValueError(msg)
    return selected


def create_app() -> FastAPI:
    """Build the app for the chassis selected by L9_CHASSIS."""
    selected = resolve_chassis()
    logger.info("Chassis selected: %s", selected)

    if selected == SDK:
        from chassis.node_app import create_app as build_sdk_app

        return build_sdk_app()

    logger.warning("Legacy dict chassis selected: direct /v1/execute ingress without Gate provenance (dev/test only)")
    from chassis.chassis_app import create_app as build_legacy_app

    return build_legacy_app()


__all__ = ["DEFAULT_CHASSIS", "LEGACY", "LEGACY_PERMITTED_ENVS", "SDK", "create_app", "resolve_chassis"]
