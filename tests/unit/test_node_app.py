"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [platform, chassis]
status: active
--- /L9_META ---

Unit tests for chassis/node_app.py and chassis/handler_registration.py —
the SDK-native chassis selected by L9_CHASSIS=sdk.

NodeRuntimeConfig is built explicitly and passed to create_node_app(config=...)
rather than read from the environment: get_runtime_config() is lru_cached and
would leak across tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from constellation_node_sdk import (
    NodeRuntimeConfig,
    clear_handlers,
    create_node_app,
    register_handler,
    registered_actions,
)
from constellation_node_sdk.transport.packet import create_transport_packet
from constellation_node_sdk.transport.provenance import RoutingProvenance
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chassis import handler_registration
from chassis.handler_registration import _with_packet_audit, register_engine_handlers
from chassis.node_app import _gate_ingress_violation, _install_gate_only_ingress
from engine.handlers import ACTION_HANDLERS

if TYPE_CHECKING:
    from collections.abc import Iterator

NODE_NAME = "graph-engine"
GATE_NODE = "gate"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """The SDK handler registry is module-global; isolate every test."""
    clear_handlers()
    yield
    clear_handlers()


@pytest.fixture(autouse=True)
def persisted_packets(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Replace PacketStore.persist with an in-memory recorder."""
    persisted: list[tuple[Any, Any]] = []

    class _Recorder:
        async def persist(self, request: Any, response: Any) -> None:
            persisted.append((request, response))

    monkeypatch.setattr(handler_registration, "get_packet_store", _Recorder)
    return persisted


def _config(**overrides: Any) -> NodeRuntimeConfig:
    base: dict[str, Any] = {
        "environment": "test",
        "node_name": NODE_NAME,
        "service_name": NODE_NAME,
        "service_version": "1.1.0",
        "require_signature": False,
        "allowed_actions": tuple(ACTION_HANDLERS),
        "max_attachments": 0,
        # The SDK's own defaults are mutually invalid (10MB attachment cap vs a
        # 256KB packet cap), so this must be set explicitly for any valid config.
        "max_attachment_size_bytes": 0,
    }
    base.update(overrides)
    # Newer constellation-node-sdk builds default enforce_gate_only_ingress=True,
    # which requires require_signature=True. These unit tests exercise unsigned
    # packets plus chassis middleware for gate-only policy, so disable the SDK
    # flag when the installed SDK exposes it.
    fields = getattr(NodeRuntimeConfig, "model_fields", {})
    if "enforce_gate_only_ingress" in fields and "enforce_gate_only_ingress" not in base:
        base["enforce_gate_only_ingress"] = False
    return NodeRuntimeConfig(**base)


def _gate_packet(action: str = "match", tenant: str = "plasticos", **payload: Any) -> dict[str, Any]:
    """Build a Gate-authored request packet as a JSON-safe dict."""
    packet = create_transport_packet(
        action=action,
        payload=payload or {"query": {}},
        tenant=tenant,
        source_node=GATE_NODE,
        destination_node=NODE_NAME,
        reply_to=GATE_NODE,
        provenance=RoutingProvenance(
            origin_kind="gate",
            requested_action=action,
            resolved_by_gate=True,
        ),
    )
    return packet.model_dump_json_dict()


def _app(config: NodeRuntimeConfig | None = None, *, gate_only: bool = True):
    app = create_node_app(config=config or _config(), auto_register_with_gate=False)
    if gate_only:
        _install_gate_only_ingress(app, gate_node=GATE_NODE)
    return app


# ── Handler registry ────────────────────────────────────────────────────────


def test_registered_actions_match_action_handlers() -> None:
    register_engine_handlers()
    assert set(registered_actions()) == set(ACTION_HANDLERS)


def test_audit_wrapper_keeps_two_parameter_signature() -> None:
    """SDK _invoke_handler dispatches on parameter count — *args would misroute."""
    import inspect

    async def handler(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    wrapped = _with_packet_audit("match", handler)
    assert len(inspect.signature(wrapped).parameters) == 2


# ── Routing and tenant invariant ────────────────────────────────────────────


def test_gate_packet_routes_to_handler_and_returns_response() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append((tenant, payload))
        return {"matches": []}

    register_handler("match", _with_packet_audit("match", spy))

    with TestClient(_app()) as client:
        response = client.post("/v1/execute", json=_gate_packet(query={"a": 1}))

    assert response.status_code == 200
    assert response.json()["header"]["packet_type"] == "response"
    assert len(seen) == 1


def test_tenant_org_id_passthrough() -> None:
    """packet.tenant.org_id is the CEG domain_id — the DomainPackLoader key."""
    seen: list[str] = []

    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(tenant)
        return {}

    register_handler("match", _with_packet_audit("match", spy))

    with TestClient(_app()) as client:
        client.post("/v1/execute", json=_gate_packet(tenant="plasticos"))

    assert seen == ["plasticos"]


def test_packet_store_persists_once_per_execute(
    persisted_packets: list[tuple[Any, Any]],
) -> None:
    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    register_handler("match", _with_packet_audit("match", spy))

    with TestClient(_app()) as client:
        client.post("/v1/execute", json=_gate_packet())

    assert len(persisted_packets) == 1


def test_failing_handler_persists_pair_and_returns_failure_packet(
    persisted_packets: list[tuple[Any, Any]],
) -> None:
    async def boom(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        msg = "handler exploded"
        raise RuntimeError(msg)

    register_handler("match", _with_packet_audit("match", boom))

    with TestClient(_app()) as client:
        response = client.post("/v1/execute", json=_gate_packet())

    # return_transport_errors=true: a transport failure packet, not HTTP 500.
    assert response.status_code == 200
    assert response.json()["header"]["packet_type"] == "failure"
    assert len(persisted_packets) == 1


# ── Gate-only ingress ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provenance", "address", "fragment"),
    [
        (
            {"origin_kind": "node", "requested_action": "match", "resolved_by_gate": True},
            {"source_node": "gate", "destination_node": NODE_NAME, "reply_to": "gate"},
            "origin_kind",
        ),
        (
            {"origin_kind": "gate", "requested_action": "match", "resolved_by_gate": False},
            {"source_node": "gate", "destination_node": NODE_NAME, "reply_to": "gate"},
            "resolved_by_gate",
        ),
        (
            {"origin_kind": "gate", "requested_action": "match", "resolved_by_gate": True},
            {"source_node": "client", "destination_node": NODE_NAME, "reply_to": "client"},
            "source_node",
        ),
    ],
)
def test_gate_ingress_violation_detects(
    provenance: dict[str, Any],
    address: dict[str, Any],
    fragment: str,
) -> None:
    reason = _gate_ingress_violation({"provenance": provenance, "address": address}, GATE_NODE)
    assert reason is not None
    assert fragment in reason


def test_gate_authored_packet_passes_violation_check() -> None:
    body = _gate_packet()
    assert _gate_ingress_violation(body, GATE_NODE) is None


def test_non_gate_packet_rejected_with_403() -> None:
    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    register_handler("match", _with_packet_audit("match", spy))

    client_packet = create_transport_packet(
        action="match",
        payload={"query": {}},
        tenant="plasticos",
        source_node="client",
        destination_node=NODE_NAME,
    ).model_dump_json_dict()

    with TestClient(_app()) as client:
        response = client.post("/v1/execute", json=client_packet)

    assert response.status_code == 403
    assert "gate-only ingress" in response.json()["detail"]


def test_gate_only_disabled_accepts_client_packet() -> None:
    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    register_handler("match", _with_packet_audit("match", spy))

    client_packet = create_transport_packet(
        action="match",
        payload={"query": {}},
        tenant="plasticos",
        source_node="client",
        destination_node=NODE_NAME,
    ).model_dump_json_dict()

    with TestClient(_app(gate_only=False)) as client:
        response = client.post("/v1/execute", json=client_packet)

    assert response.status_code == 200


# ── Ingress surface (CONTRACT-01: single ingress) ────────────────────────────


def test_core_ingress_routes_present() -> None:
    """SDK chassis always exposes execute/health/metrics; newer SDKs may also mount relay."""
    paths = {route.path for route in _app().routes if hasattr(route, "path")}
    assert {"/v1/execute", "/v1/health", "/metrics"} <= paths


# ── Readiness ───────────────────────────────────────────────────────────────


def test_health_reports_not_ready_before_lifespan() -> None:
    """The SDK health route is always HTTP 200 — readiness lives in `ready`."""
    response = TestClient(_app()).get("/v1/health")
    assert response.status_code == 200
    assert response.json()["ready"] is False


# ── Preflight fails closed ──────────────────────────────────────────────────


def test_require_signature_without_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="signing_key"):
        _config(require_signature=True, signing_key=None)


def test_max_attachments_without_schemes_is_rejected() -> None:
    with pytest.raises(ValidationError, match="attachment_allowed_schemes"):
        _config(max_attachments=4, max_attachment_size_bytes=1024)


def test_sdk_default_attachment_caps_construct() -> None:
    """Pinned SDK (69c6c67 = main a0827f2 + verifying-keys env fix) ships mutually valid default attachment/packet caps.

    Earlier pins (a770e853) defaulted max_attachment_size_bytes above
    max_packet_bytes and failed closed on a bare construct. The current pin
    defaults attachments off (0 / 0), so a bare construct is valid and the
    invariant max_attachment_size_bytes <= max_packet_bytes still holds.
    """
    bare = NodeRuntimeConfig(
        environment="test",
        node_name=NODE_NAME,
        service_name=NODE_NAME,
        service_version="1.1.0",
    )
    assert bare.max_attachments == 0
    assert bare.max_attachment_size_bytes == 0
    assert bare.max_attachment_size_bytes <= bare.max_packet_bytes
    cfg = _config()
    assert cfg.max_packet_bytes > 0
    assert cfg.max_attachment_size_bytes >= 0
    assert cfg.max_attachment_size_bytes <= cfg.max_packet_bytes
