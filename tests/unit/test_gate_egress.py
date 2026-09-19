"""Unit tests for engine/gate_egress.py (CEG -> Gate -> EIE `enrich`)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("constellation_node_sdk", reason="constellation-node-sdk not installed")

from constellation_node_sdk import GateConnectionError, create_transport_packet

from engine import gate_egress
from engine.gate_egress import (
    build_enrichment_request,
    enrichment_idempotency_key,
    request_enrichment,
)

pytestmark = pytest.mark.unit


class _FakeClient:
    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._error = error

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _response_packet(packet_type: str = "response") -> Any:
    packet = create_transport_packet(
        action="enrich",
        payload={"state": "completed", "fields": {"polymer": "HDPE"}},
        tenant="acme",
        destination_node="graph",
        source_node="gate",
    )
    return packet.derive(packet_type=packet_type)


def test_build_enrichment_request_matches_eie_enrich_request_shape():
    payload = build_enrichment_request(
        entity_id="ent-1", domain="plasticos", target_fields=["polymer", "capacity", "polymer"], entity={"name": "Acme"}
    )
    assert set(payload) == {"entity", "object_type", "schema", "objective", "kb_context"}
    assert payload["entity"] == {"entity_id": "ent-1", "domain": "plasticos", "name": "Acme"}
    assert payload["object_type"] == "plasticos"
    assert payload["schema"] == {"polymer": "string", "capacity": "string"}
    assert payload["kb_context"] == "plasticos"
    assert "ent-1" in payload["objective"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entity_id": "", "domain": "d", "target_fields": ["f"]},
        {"entity_id": "e", "domain": "", "target_fields": ["f"]},
        {"entity_id": "e", "domain": "d", "target_fields": []},
    ],
)
def test_build_enrichment_request_rejects_incomplete_input(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        build_enrichment_request(**kwargs)


def test_idempotency_key_is_stable_and_order_independent():
    a = enrichment_idempotency_key("acme", "ent-1", ["x", "y"])
    b = enrichment_idempotency_key("acme", "ent-1", ["y", "x"])
    assert a == b
    assert a.startswith("ceg:enrich:acme:ent-1:")
    assert enrichment_idempotency_key("acme", "ent-2", ["x", "y"]) != a


async def test_request_enrichment_fails_closed_without_gate_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GATE_URL", raising=False)
    fake = _FakeClient(response=_response_packet())
    monkeypatch.setattr(gate_egress, "get_gate_client", lambda: fake)
    result = await request_enrichment(tenant="acme", entity_id="ent-1", domain="plasticos", target_fields=["polymer"])
    assert result == {"status": "failed", "error": "gate_not_configured", "action": "enrich"}
    assert fake.calls == [], "no direct fallback: nothing may be sent when Gate is not configured"


async def test_request_enrichment_sends_enrich_action_through_gate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GATE_URL", "http://gate:9000")
    fake = _FakeClient(response=_response_packet())
    monkeypatch.setattr(gate_egress, "get_gate_client", lambda: fake)

    result = await request_enrichment(
        tenant="acme", entity_id="ent-1", domain="plasticos", target_fields=["polymer"], timeout_ms=5_000
    )

    assert len(fake.calls) == 1, "exactly one attempt per call"
    call = fake.calls[0]
    assert call["action"] == "enrich"
    assert call["tenant"] == "acme"
    assert call["timeout_ms"] == 5_000
    assert call["idempotency_key"] == enrichment_idempotency_key("acme", "ent-1", ["polymer"])
    assert call["payload"]["object_type"] == "plasticos"
    assert result["status"] == "ok"
    assert result["payload"]["state"] == "completed"
    assert result["idempotency_key"] == call["idempotency_key"]


async def test_request_enrichment_reports_sdk_errors_as_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GATE_URL", "http://gate:9000")
    fake = _FakeClient(error=GateConnectionError("gate unreachable"))
    monkeypatch.setattr(gate_egress, "get_gate_client", lambda: fake)
    result = await request_enrichment(tenant="acme", entity_id="ent-1", domain="plasticos", target_fields=["polymer"])
    assert result["status"] == "failed"
    assert result["error"] == "GateConnectionError"
    assert len(fake.calls) == 1


async def test_request_enrichment_treats_failure_packet_as_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GATE_URL", "http://gate:9000")
    fake = _FakeClient(response=_response_packet(packet_type="failure"))
    monkeypatch.setattr(gate_egress, "get_gate_client", lambda: fake)
    result = await request_enrichment(tenant="acme", entity_id="ent-1", domain="plasticos", target_fields=["polymer"])
    assert result["status"] == "failed"
    assert result["packet_type"] == "failure"
