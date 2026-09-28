"""
Gate participation is owned by Gate_SDK, not by CEG (L9-PARTICIPATION-01).

Verifies:
- CEG's own registration module and its re-registration settings are gone
- the SDK chassis leaves registration on, so create_node_app() registers,
  re-registers after a Gate restart, and binds /v1/ready to Gate's acceptance
- GraphLifecycle no longer registers (no double registration)
"""

from __future__ import annotations

import importlib.util
import inspect
from typing import Any

import pytest
from fastapi import FastAPI

pytest.importorskip("constellation_node_sdk", reason="constellation-node-sdk not installed")


def test_ceg_registration_loop_is_retired() -> None:
    from engine.config.settings import Settings

    assert importlib.util.find_spec("engine.gate_registration") is None
    for field in ("gate_reregistration_enabled", "gate_reregistration_interval_seconds"):
        assert field not in Settings.model_fields, f"Settings.{field} must not exist"


def test_graph_lifecycle_does_not_register() -> None:
    from engine.boot import GraphLifecycle

    source = inspect.getsource(GraphLifecycle)
    for name in ("register_from_env", "register_node", "gate_registration"):
        assert name not in source, f"GraphLifecycle still calls {name}"


def test_sdk_chassis_leaves_participation_to_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    from chassis import node_app

    captured: dict[str, Any] = {}

    def spy(**kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    monkeypatch.setattr(node_app, "create_node_app", spy)
    monkeypatch.setenv("L9_ENFORCE_GATE_ONLY_INGRESS", "false")

    node_app.create_app()

    assert captured.get("auto_register_with_gate", True) is True
    assert isinstance(captured["lifecycle_hook"], node_app.SdkLifecycleAdapter)


def test_sdk_runtime_exposes_participation_readiness() -> None:
    from constellation_node_sdk import create_node_app

    parameters = inspect.signature(create_node_app).parameters
    assert "registration" in parameters, "Gate_SDK >= 1.2.0 (L9-PARTICIPATION-01) is required"

    app = create_node_app(auto_register_with_gate=False)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/v1/ready" in paths
