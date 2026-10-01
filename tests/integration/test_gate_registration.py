"""
Gate participation defaults to Gate_SDK (L9-PARTICIPATION-01).

Verifies:
- sdk_participation_enabled defaults on, and the CEG loop remains for rollback
- the SDK chassis leaves registration on while that flag is on
- the SDK chassis disables its own registration when the flag is off
- GraphLifecycle registers only on the rollback path
- the installed SDK exposes participation readiness
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import FastAPI

pytest.importorskip("constellation_node_sdk", reason="constellation-node-sdk not installed")


def test_sdk_participation_defaults_on_and_keeps_a_rollback() -> None:
    from engine.config.settings import Settings
    from engine.gate_registration import register_node_with_gate

    fields = Settings.model_fields
    assert fields["sdk_participation_enabled"].default is True
    assert fields["gate_reregistration_enabled"].default is True
    assert fields["gate_reregistration_interval_seconds"].default == 300.0
    assert inspect.iscoroutinefunction(register_node_with_gate)


def test_graph_lifecycle_registers_only_when_participation_is_off() -> None:
    from engine.boot import GraphLifecycle

    source = inspect.getsource(GraphLifecycle)
    assert "if not settings.sdk_participation_enabled:" in source
    assert "register_node_with_gate" in source


def test_sdk_chassis_follows_the_participation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from chassis import node_app
    from engine.config.settings import settings

    captured: dict[str, Any] = {}

    def spy(**kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    monkeypatch.setattr(node_app, "create_node_app", spy)
    monkeypatch.setenv("L9_ENFORCE_GATE_ONLY_INGRESS", "false")

    monkeypatch.setattr(settings, "sdk_participation_enabled", True)
    node_app.create_app()
    assert captured["auto_register_with_gate"] is True
    assert isinstance(captured["lifecycle_hook"], node_app.SdkLifecycleAdapter)

    monkeypatch.setattr(settings, "sdk_participation_enabled", False)
    node_app.create_app()
    assert captured["auto_register_with_gate"] is False


def test_sdk_runtime_exposes_participation_readiness() -> None:
    from constellation_node_sdk import create_node_app

    parameters = inspect.signature(create_node_app).parameters
    assert "registration" in parameters, "Gate_SDK >= 1.2.0 (L9-PARTICIPATION-01) is required"

    app = create_node_app(auto_register_with_gate=False)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/v1/ready" in paths
