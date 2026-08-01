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

    L9_CHASSIS=legacy  (default) -> chassis.chassis_app.create_app
    L9_CHASSIS=sdk               -> chassis.node_app.create_app

Every launch site (scripts/entrypoint.sh, Dockerfile.prod, Makefile) points
at chassis.entrypoint:create_app so switching chassis is a config change,
not a command change.
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
_VALID = (LEGACY, SDK)


def resolve_chassis() -> str:
    """Return the selected chassis name, validating L9_CHASSIS."""
    selected = os.environ.get("L9_CHASSIS", LEGACY).strip().lower()
    if selected not in _VALID:
        msg = f"L9_CHASSIS must be one of {_VALID}, got {selected!r}"
        raise ValueError(msg)
    return selected


def create_app() -> FastAPI:
    """Build the app for the chassis selected by L9_CHASSIS."""
    selected = resolve_chassis()
    logger.info("Chassis selected: %s", selected)

    if selected == SDK:
        from chassis.node_app import create_app as build_sdk_app

        return build_sdk_app()

    from chassis.chassis_app import create_app as build_legacy_app

    return build_legacy_app()


__all__ = ["LEGACY", "SDK", "create_app", "resolve_chassis"]
