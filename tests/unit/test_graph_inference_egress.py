"""EIE-008 / CEG-006 — the graph-inference feedback loop needs a producer.

EIE advertises `graph-inference-result` to Gate and implements the entire
consumer side: packet validation, per-tenant queues, target extraction, a 0.55
confidence floor, and injection into the convergence loop. No code in CEG ever
constructed such a packet, so the loop the architecture implies had a consumer
and nothing on the other end — and the two halves were built to the same
confidence floor without ever being connected.

The mirror finding: CEG's own outbound `request_enrichment` was reachable only
through `engine/health/api.py`, which nothing imported, so no inbound packet
could cause CEG to call EIE either.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.gate_egress import (
    GRAPH_INFERENCE_ACTION,
    INFERENCE_CONFIDENCE_FLOOR,
    build_inference_outputs,
    emit_graph_inference_result,
    inference_idempotency_key,
)
from engine.inference_rule_registry import InferenceResult


def _result(field: str, value: Any, confidence: float, rule: str = "r") -> InferenceResult:
    return InferenceResult(field_name=field, value=value, confidence=confidence, rule_name=rule)


# ── Output shaping: EIE's contract is the spec ──────────────────────────────


def test_outputs_carry_every_key_eie_requires() -> None:
    """EIE's extract_targets_from_packet reads entity_id, field, value,
    confidence and rule off each element."""
    outputs = build_inference_outputs("e-1", [_result("tier", "mid_market", 0.85, "size_rule")])
    assert len(outputs) == 1
    assert outputs[0]["entity_id"] == "e-1"
    assert outputs[0]["field"] == "tier"
    assert outputs[0]["value"] == "mid_market"
    assert outputs[0]["confidence"] == 0.85
    assert outputs[0]["rule"] == "size_rule"


def test_the_floor_matches_the_receiver() -> None:
    """EIE drops below 0.55 (graph_return_channel.CONFIDENCE_FLOOR); so do we,
    so a packet never carries outputs the receiver will silently discard."""
    assert INFERENCE_CONFIDENCE_FLOOR == 0.55
    outputs = build_inference_outputs(
        "e-1",
        [_result("kept", "a", 0.56), _result("dropped", "b", 0.54)],
    )
    assert [o["field"] for o in outputs] == ["kept"]


def test_plain_mappings_are_accepted_too() -> None:
    outputs = build_inference_outputs("e-1", [{"field": "f", "value": 1, "confidence": 0.9}])
    assert outputs[0]["rule"] == "unknown"


@pytest.mark.parametrize(
    "bad",
    [{"value": 1, "confidence": 0.9}, {"field": "f", "value": 1, "confidence": "high"}],
)
def test_malformed_outputs_are_dropped_not_sent(bad: dict) -> None:
    assert build_inference_outputs("e-1", [bad]) == []


def test_idempotency_key_is_stable_over_the_same_findings() -> None:
    a = build_inference_outputs("e-1", [_result("f", "v", 0.9)])
    b = build_inference_outputs("e-1", [_result("f", "v", 0.9)])
    assert inference_idempotency_key("t", "e-1", a) == inference_idempotency_key("t", "e-1", b)


def test_idempotency_key_changes_when_the_finding_changes() -> None:
    a = build_inference_outputs("e-1", [_result("f", "v1", 0.9)])
    b = build_inference_outputs("e-1", [_result("f", "v2", 0.9)])
    assert inference_idempotency_key("t", "e-1", a) != inference_idempotency_key("t", "e-1", b)


# ── Dispatch ────────────────────────────────────────────────────────────────


class _FakeHeader:
    packet_id = "pkt-1"
    packet_type = "response"
    correlation_id = None


class _FakeResponse:
    header = _FakeHeader()
    payload: dict[str, Any] = {"queued": 1}


@pytest.mark.asyncio
async def test_nothing_above_the_floor_sends_no_packet(monkeypatch) -> None:
    """An empty inference_outputs list is valid to EIE and would cost a Gate
    round trip to queue nothing."""

    def _no_client():
        msg = "the Gate client must not be constructed"
        raise AssertionError(msg)

    monkeypatch.setenv("GATE_URL", "http://gate.test")
    monkeypatch.setattr("engine.gate_egress.get_gate_client", _no_client)

    result = await emit_graph_inference_result(tenant="t", entity_id="e-1", results=[_result("f", "v", 0.1)])
    assert result["status"] == "skipped"
    assert result["sent_outputs"] == 0


@pytest.mark.asyncio
async def test_unconfigured_gate_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("GATE_URL", raising=False)
    result = await emit_graph_inference_result(tenant="t", entity_id="e-1", results=[_result("f", "v", 0.9)])
    assert result["status"] == "failed"
    assert result["error"] == "gate_not_configured"


@pytest.mark.asyncio
async def test_a_successful_emission_addresses_gate_with_eies_action(monkeypatch) -> None:
    sent: dict[str, Any] = {}

    class _Client:
        async def execute(self, **kwargs: Any) -> _FakeResponse:
            sent.update(kwargs)
            return _FakeResponse()

    monkeypatch.setenv("GATE_URL", "http://gate.test")
    monkeypatch.setattr("engine.gate_egress.get_gate_client", _Client)

    result = await emit_graph_inference_result(tenant="acme", entity_id="e-1", results=[_result("tier", "small", 0.9)])

    assert result["status"] == "ok"
    assert result["sent_outputs"] == 1
    assert sent["action"] == GRAPH_INFERENCE_ACTION
    assert sent["tenant"] == "acme"
    assert sent["payload"]["inference_outputs"][0]["field"] == "tier"
    # CEG never addresses EIE: the destination is Gate, resolved by action.
    assert "destination_node" not in sent
