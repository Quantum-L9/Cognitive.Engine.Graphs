"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [platform, chassis]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for chassis/node_app.py and chassis/handler_registration.py —
the SDK-native chassis selected by L9_CHASSIS=sdk.

NodeRuntimeConfig is built explicitly and passed to create_node_app(config=...)
rather than read from the environment: get_runtime_config() is lru_cached and
would leak across tests.
"""

from __future__ import annotations

import asyncio
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


def test_audit_wrapper_takes_explicit_packet_aware_signature() -> None:
    """SDK _invoke_handler dispatches on parameter count — *args would misroute.

    Three explicit parameters select the SDK's ``(org_id, payload, packet)`` form,
    which hands the wrapper the ingress packet it audits (CEG-FIX-002).
    """
    import inspect

    async def handler(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    parameters = list(inspect.signature(_with_packet_audit("match", handler)).parameters.values())
    assert [p.name for p in parameters] == ["tenant", "payload", "packet"]
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in parameters)


def test_audit_wrapper_flag_off_restores_reconstructed_request(
    monkeypatch: pytest.MonkeyPatch,
    persisted_packets: list[tuple[Any, Any]],
) -> None:
    """INGRESS_PACKET_AUDIT_ENABLED=false is the operator rollback for CEG-FIX-002."""
    import inspect

    from engine.config.settings import settings

    monkeypatch.setattr(settings, "ingress_packet_audit_enabled", False)

    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    wrapped = _with_packet_audit("match", spy)
    assert [p.name for p in inspect.signature(wrapped).parameters.values()] == ["tenant", "payload"]

    register_handler("match", wrapped)
    ingress = _identified_gate_packet()
    with TestClient(_app()) as client:
        response = client.post("/v1/execute", json=ingress.model_dump_json_dict())

    assert response.status_code == 200
    ((request, _response),) = persisted_packets
    assert request.header.packet_id != ingress.header.packet_id
    assert request.header.trace_id != "trace-ceg-fix-002"


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


# ── Handler cancellation (RU-086) ───────────────────────────────────────────


async def _never_returns(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(3600)
    return {"unreachable": True}


def _cancel_ingress_packet() -> Any:
    return create_transport_packet(
        action="match",
        payload={"query": {}},
        tenant="plasticos",
        source_node=GATE_NODE,
        destination_node=NODE_NAME,
        reply_to=GATE_NODE,
        provenance=RoutingProvenance(origin_kind="gate", requested_action="match", resolved_by_gate=True),
    )


async def test_timeout_cancelled_handler_persists_one_failed_pair(
    persisted_packets: list[tuple[Any, Any]],
) -> None:
    """Port of repro r6_audit_timeout.py: the SDK cancels a handler on timeout."""
    audited = _with_packet_audit("match", _never_returns)
    packet = _cancel_ingress_packet()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(audited("plasticos", {"query": {}}, packet), timeout=0.05)

    pairs = [(resp.header.action, resp.payload["status"]) for _req, resp in persisted_packets]
    assert pairs == [("match", "failed")]
    ((request, response),) = persisted_packets
    assert request.header.packet_id == packet.header.packet_id
    assert response.payload["data"] == {"error": "handler_cancelled"}


async def test_cancel_audit_flag_off_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
    persisted_packets: list[tuple[Any, Any]],
) -> None:
    monkeypatch.setattr(handler_registration.settings, "packet_audit_on_cancel_enabled", False)
    audited = _with_packet_audit("match", _never_returns)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(audited("plasticos", {"query": {}}, _cancel_ingress_packet()), timeout=0.05)

    assert persisted_packets == []


async def test_cancel_audit_store_failure_still_reraises_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        async def persist(self, request: Any, response: Any) -> None:
            msg = "store down"
            raise RuntimeError(msg)

    monkeypatch.setattr(handler_registration, "get_packet_store", _Broken)
    audited = _with_packet_audit("match", _never_returns)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(audited("plasticos", {"query": {}}, _cancel_ingress_packet()), timeout=0.05)


# ── Ingress provenance (CEG-FIX-002) ────────────────────────────────────────


def _identified_gate_packet() -> Any:
    """A Gate-authored ingress packet with a known trace and correlation."""
    return create_transport_packet(
        action="match",
        payload={"query": {}},
        tenant="plasticos",
        source_node=GATE_NODE,
        destination_node=NODE_NAME,
        reply_to=GATE_NODE,
        trace_id="trace-ceg-fix-002",
        correlation_id="corr-ceg-fix-002",
        provenance=RoutingProvenance(
            origin_kind="gate",
            requested_action="match",
            resolved_by_gate=True,
        ),
    )


@pytest.mark.finding("CEG-FIX-002")
def test_audited_request_is_the_ingress_packet(
    persisted_packets: list[tuple[Any, Any]],
) -> None:
    """The persisted request carries the ingress identity, not a reconstruction.

    Gate_SDK holds one trace_id, one correlation_id and one root_id across every
    generation of the rail (tests/integration/test_canonical_round_trip_specimen.py
    @ 7ec6cdf); a freshly created packet starts a new root and a new trace.
    """

    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    register_handler("match", _with_packet_audit("match", spy))
    ingress = _identified_gate_packet()

    with TestClient(_app()) as client:
        client.post("/v1/execute", json=ingress.model_dump_json_dict())

    ((request, _response),) = persisted_packets
    assert request.header.packet_id == ingress.header.packet_id
    assert request.header.trace_id == "trace-ceg-fix-002"
    assert request.header.correlation_id == "corr-ceg-fix-002"
    assert request.lineage.root_id == ingress.lineage.root_id
    assert request.tenant == ingress.tenant
    assert request.provenance == ingress.provenance


@pytest.mark.finding("CEG-FIX-002")
@pytest.mark.parametrize("fails", [False, True])
def test_audited_response_derives_from_ingress(
    persisted_packets: list[tuple[Any, Any]],
    fails: bool,
) -> None:
    """Success and failure audit responses stay on the ingress lineage and trace."""

    async def handler(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        if fails:
            msg = "handler exploded"
            raise RuntimeError(msg)
        return {"ok": True}

    register_handler("match", _with_packet_audit("match", handler))
    ingress = _identified_gate_packet()

    with TestClient(_app()) as client:
        wire = client.post("/v1/execute", json=ingress.model_dump_json_dict()).json()

    ((_request, response),) = persisted_packets
    assert response.lineage.parent_id == ingress.header.packet_id
    assert response.lineage.root_id == ingress.lineage.root_id
    assert response.header.causation_id == ingress.header.packet_id
    assert response.header.trace_id == "trace-ceg-fix-002"
    assert response.header.correlation_id == "corr-ceg-fix-002"
    assert response.payload["meta"]["trace_id"] == "trace-ceg-fix-002"
    # The audit pair and the SDK's own wire response describe one request.
    assert wire["header"]["trace_id"] == response.header.trace_id
    assert wire["header"]["correlation_id"] == response.header.correlation_id
    assert wire["lineage"]["root_id"] == str(response.lineage.root_id)


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
    """Pinned SDK (@v1 / 1.1.0) ships mutually valid default attachment/packet caps.

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


# ── GATE-KEY-01: a Gate claim must be signed under a Gate key id ─────────────

GATE_KEY_ID = "gate-e2e"
PEER_KEY_ID = "eie-e2e"
GATE_HMAC = "gate-secret-0123456789abcdef0123456789"
PEER_HMAC = "eie-secret-0123456789abcdef01234567890"


def _ed25519_pair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def _signed_gate_claim(tenant: str, *, key: str, key_id: str, algorithm: str = "hmac-sha256") -> dict[str, Any]:
    """A packet claiming Gate authorship, signed by whoever holds ``key``."""
    from constellation_node_sdk.security.signing import sign_transport_packet

    packet = create_transport_packet(
        action="match",
        payload={"query": {}},
        tenant=tenant,
        source_node=GATE_NODE,
        destination_node=NODE_NAME,
        reply_to=GATE_NODE,
        provenance=RoutingProvenance(
            origin_kind="gate",
            requested_action="match",
            resolved_by_gate=True,
            route_kind="external_ingress",
        ),
    )
    return sign_transport_packet(packet, key=key, key_id=key_id, algorithm=algorithm).model_dump_json_dict()


def _bound_app(verifying_keys: dict[str, str], *, refuse_symmetric: bool = False) -> Any:
    config = _config(
        require_signature=True,
        signing_algorithm="hmac-sha256",
        signing_key="ceg-secret-0123456789abcdef01234567890",
        signing_key_id="ceg-e2e",
        verifying_keys=verifying_keys,
    )
    app = create_node_app(config=config, auto_register_with_gate=False)
    _install_gate_only_ingress(
        app,
        gate_node=GATE_NODE,
        gate_key_ids=frozenset({GATE_KEY_ID}),
        refuse_symmetric=refuse_symmetric,
    )
    return app


def _tenant_spy() -> list[str]:
    seen: list[str] = []

    async def spy(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(tenant)
        return {"status": "completed"}

    register_handler("match", spy)
    return seen


def test_peer_key_cannot_claim_gate_and_choose_tenant() -> None:
    """U-TRANSPORT: a peer key CEG verifies, claiming Gate, never reaches a handler."""
    seen = _tenant_spy()
    keys = {GATE_KEY_ID: GATE_HMAC, PEER_KEY_ID: PEER_HMAC}
    with TestClient(_bound_app(keys)) as client:
        forged = client.post("/v1/execute", json=_signed_gate_claim("victim", key=PEER_HMAC, key_id=PEER_KEY_ID))
        genuine = client.post("/v1/execute", json=_signed_gate_claim("acme", key=GATE_HMAC, key_id=GATE_KEY_ID))

    assert forged.status_code == 403
    assert "not a Gate signing key" in forged.json()["detail"]
    assert genuine.status_code == 200
    assert seen == ["acme"]


def test_unsigned_gate_claim_refused_when_bound() -> None:
    seen = _tenant_spy()
    with TestClient(_bound_app({GATE_KEY_ID: GATE_HMAC})) as client:
        response = client.post("/v1/execute", json=_gate_packet())

    assert response.status_code == 403
    assert seen == []


def test_symmetric_gate_signature_refused_when_trust_required() -> None:
    """Every verifier of a shared HMAC key can mint it, so staging/prod refuse it."""
    seen = _tenant_spy()
    with TestClient(_bound_app({GATE_KEY_ID: GATE_HMAC}, refuse_symmetric=True)) as client:
        response = client.post("/v1/execute", json=_signed_gate_claim("victim", key=GATE_HMAC, key_id=GATE_KEY_ID))

    assert response.status_code == 403
    assert "ed25519" in response.json()["detail"]
    assert seen == []


def test_ed25519_gate_signature_accepted_when_trust_required() -> None:
    private_pem, public_pem = _ed25519_pair()
    seen = _tenant_spy()
    with TestClient(_bound_app({GATE_KEY_ID: public_pem}, refuse_symmetric=True)) as client:
        response = client.post(
            "/v1/execute",
            json=_signed_gate_claim("acme", key=private_pem, key_id=GATE_KEY_ID, algorithm="ed25519"),
        )

    assert response.status_code == 200
    assert seen == ["acme"]


def test_gate_key_violation_needs_security_block() -> None:
    reason = _gate_ingress_violation(
        {k: v for k, v in _gate_packet().items() if k != "security"},
        GATE_NODE,
        gate_key_ids=frozenset({GATE_KEY_ID}),
    )
    assert reason == "packet must carry security"


@pytest.mark.parametrize(
    ("sdk_env", "l9_env", "raw", "expected"),
    [
        ("local", "dev", "", (None, False)),
        ("local", "dev", " gate-a , gate-b ", (frozenset({"gate-a", "gate-b"}), False)),
        ("staging", "dev", "gate-a", (frozenset({"gate-a"}), True)),
        ("local", "prod", "gate-a", (frozenset({"gate-a"}), True)),
    ],
)
def test_gate_key_binding_resolution(
    monkeypatch: pytest.MonkeyPatch,
    sdk_env: str,
    l9_env: str,
    raw: str,
    expected: tuple[frozenset[str] | None, bool],
) -> None:
    from chassis.node_app import _gate_key_binding
    from engine.config.settings import settings

    monkeypatch.setenv("L9_ENVIRONMENT", sdk_env)
    monkeypatch.setenv("L9_GATE_SIGNING_KEY_IDS", raw)
    monkeypatch.setattr(settings, "l9_env", l9_env)
    assert _gate_key_binding() == expected


@pytest.mark.parametrize(
    ("sdk_env", "l9_env"),
    [("staging", "dev"), ("prod", "dev"), ("production", "dev"), ("local", "prod")],
)
def test_gate_key_binding_required_in_trust_environments(
    monkeypatch: pytest.MonkeyPatch, sdk_env: str, l9_env: str
) -> None:
    from chassis.node_app import _gate_key_binding
    from engine.config.settings import settings

    monkeypatch.setenv("L9_ENVIRONMENT", sdk_env)
    monkeypatch.delenv("L9_GATE_SIGNING_KEY_IDS", raising=False)
    monkeypatch.setattr(settings, "l9_env", l9_env)
    with pytest.raises(RuntimeError, match="L9_GATE_SIGNING_KEY_IDS is required"):
        _gate_key_binding()


def test_gate_key_binding_flag_off_restores_claim_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from chassis.node_app import _gate_key_binding
    from engine.config.settings import settings

    monkeypatch.setenv("L9_ENVIRONMENT", "prod")
    monkeypatch.delenv("L9_GATE_SIGNING_KEY_IDS", raising=False)
    monkeypatch.setattr(settings, "gate_signing_key_binding_enabled", False)
    assert _gate_key_binding() == (None, False)
