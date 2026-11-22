"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [integration]
tags: [gate, transport, outbound, sdk, enrichment]
owner: engine-team
status: active
--- /L9_META ---

engine/gate_egress.py — the only CEG -> peer egress: CEG -> Gate -> EIE.

CEG never addresses the enrichment node. It asks Gate to run an EIE-owned
action (``graph-inference-result``; see Gate's CANONICAL_ACTION_OWNERS) and
receives Gate's response packet. The SDK owns packet construction, signing, the
single HTTP attempt, and the deadline derived from ``timeout_ms``.

One direction lives here: ``emit_graph_inference_result`` — CEG hands EIE
values it derived from the graph, which EIE's ``GraphReturnChannel`` feeds into
the convergence loop.

CEG sends no enrichment request. ``request_enrichment`` (the ``enrich`` action)
was retired with CEG's enrichment trigger: the trigger lives in EIE, which
reads readiness from CEG ``health-assess`` (operator decision 2026-11-15).

``emit_graph_inference_result`` closes a loop that was open in one direction only (EIE-008 /
CEG-006): EIE advertised ``graph-inference-result`` to Gate and implemented the
whole consumer side — validation, per-tenant queues, target extraction — while
no code in CEG ever constructed such a packet. The two halves were even built
to the same 0.55 confidence floor and never connected.

Fail-closed rules (seam audit 2026-09-02):
  * no GATE_URL -> ``gate_not_configured``; there is no direct fallback;
  * one attempt per call; retry is the caller's decision and requires the
    idempotency key returned in the result;
  * every SDK error is reported as a typed failure, never swallowed as success.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from collections.abc import Sequence
from typing import Any

from constellation_node_sdk import GateClientError

from engine.gate_client import get_gate_client

logger = logging.getLogger(__name__)

GRAPH_INFERENCE_ACTION = "graph-inference-result"
DEFAULT_INFERENCE_TIMEOUT_MS = 10_000
_SEAM_TAGS: tuple[str, ...] = ("INTER_NODE",)

# EIE drops any inference output below this confidence
# (app/services/graph_return_channel.py CONFIDENCE_FLOOR). CEG's own rule
# registry suppresses below the same number (InferenceContext.confidence_floor).
# Filtering here too means a packet never carries outputs the receiver will
# silently discard, so "sent 6, queued 2" is visible at the sender.
INFERENCE_CONFIDENCE_FLOOR = 0.55


def inference_idempotency_key(tenant: str, entity_id: str, outputs: Sequence[dict[str, Any]]) -> str:
    """Stable over the same findings, so a retry cannot double-queue targets."""
    parts = sorted(f"{o.get('field')}={o.get('value')!r}@{o.get('confidence')}" for o in outputs)
    digest = hashlib.sha256("|".join([tenant, entity_id, *parts]).encode("utf-8")).hexdigest()
    return f"ceg:graph-inference:{tenant}:{entity_id}:{digest[:16]}"


def build_inference_outputs(
    entity_id: str,
    results: Sequence[Any],
) -> list[dict[str, Any]]:
    """Shape CEG inference results into EIE's ``inference_outputs`` elements.

    Accepts ``InferenceResult`` (engine.inference_rule_registry) or a mapping
    already in that shape. EIE requires ``entity_id``, ``field``, ``value``,
    ``confidence`` and ``rule`` per element; ``InferenceResult.to_dict()``
    already produces all but ``entity_id``.

    Outputs below ``INFERENCE_CONFIDENCE_FLOOR`` are dropped here rather than
    sent to be dropped there.
    """
    outputs: list[dict[str, Any]] = []
    for result in results:
        raw = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        field_name = raw.get("field")
        if not field_name:
            logger.warning("gate_egress: inference output without a field name — dropped")
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            logger.warning("gate_egress: non-numeric confidence for field=%s — dropped", field_name)
            continue
        # NaN compares False against the floor and would otherwise pass it; an
        # infinite or >1 confidence is not a confidence. Dropped, not clamped.
        if not math.isfinite(confidence) or confidence > 1.0:
            logger.warning("gate_egress: confidence out of [0, 1] for field=%s — dropped", field_name)
            continue
        if confidence < INFERENCE_CONFIDENCE_FLOOR:
            logger.debug(
                "gate_egress: confidence %.3f below floor %.3f for field=%s — not sent",
                confidence,
                INFERENCE_CONFIDENCE_FLOOR,
                field_name,
            )
            continue
        outputs.append(
            {
                "entity_id": entity_id,
                "field": str(field_name),
                "value": raw.get("value"),
                "confidence": confidence,
                "rule": str(raw.get("rule", "unknown")),
                "provenance": str(raw.get("provenance", "inference")),
                "rationale": str(raw.get("rationale", "")),
            }
        )
    return outputs


async def emit_graph_inference_result(
    *,
    tenant: str,
    entity_id: str,
    results: Sequence[Any],
    timeout_ms: int = DEFAULT_INFERENCE_TIMEOUT_MS,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Hand graph-derived field values to EIE via Gate. One attempt, fail closed.

    EIE's ``GraphReturnChannel`` converts each output into an ``EnrichmentTarget``
    and feeds the convergence loop, so this is the producer for the action EIE
    has advertised and implemented all along (EIE-008).

    Returns ``status`` / ``action`` / ``idempotency_key`` and, when sent, the
    response packet identity and payload, with ``sent_outputs`` so a caller can see how many findings survived the floor.
    A call with nothing above the floor is reported as ``skipped`` and sends no
    packet — an empty ``inference_outputs`` list is valid to EIE and would cost
    a Gate round trip to queue nothing.
    """
    outputs = build_inference_outputs(entity_id, results)
    # A caller that binds the key to more than the findings (the graph trigger
    # binds evidence and rule revision too) supplies it; otherwise it is derived.
    key = idempotency_key or inference_idempotency_key(tenant, entity_id, outputs)

    if not outputs:
        return {
            "status": "skipped",
            "error": "no_outputs_above_confidence_floor",
            "action": GRAPH_INFERENCE_ACTION,
            "idempotency_key": key,
            "sent_outputs": 0,
        }

    if not os.environ.get("GATE_URL", "").strip():
        logger.warning("gate_egress: GATE_URL unset — inference result for %s not sent", entity_id)
        return {
            "status": "failed",
            "error": "gate_not_configured",
            "action": GRAPH_INFERENCE_ACTION,
            "idempotency_key": key,
            "sent_outputs": 0,
        }

    try:
        # ValueError as well as GateClientError: get_gate_client() builds the SDK
        # config from the environment and raises ValueError on missing or invalid
        # material. Letting that escape would crash the admin subaction instead of
        # returning the typed failure every other path in this module returns.
        client = get_gate_client()
        response = await client.execute(
            action=GRAPH_INFERENCE_ACTION,
            payload={"inference_outputs": outputs},
            tenant=tenant,
            idempotency_key=key,
            timeout_ms=timeout_ms,
            correlation_id=correlation_id,
            compliance_tags=_SEAM_TAGS,
        )
    except (GateClientError, ValueError) as exc:
        logger.warning(
            "gate_egress: %s for inference entity=%s tenant=%s: %s",
            type(exc).__name__,
            entity_id,
            tenant,
            exc,
        )
        return {
            "status": "failed",
            "error": type(exc).__name__,
            "detail": str(exc),
            "action": GRAPH_INFERENCE_ACTION,
            "idempotency_key": key,
            "sent_outputs": 0,
        }

    failed = response.header.packet_type == "failure"
    return {
        "status": "failed" if failed else "ok",
        "action": GRAPH_INFERENCE_ACTION,
        "idempotency_key": key,
        "sent_outputs": len(outputs),
        "packet_id": str(response.header.packet_id),
        "packet_type": response.header.packet_type,
        "correlation_id": str(response.header.correlation_id) if response.header.correlation_id else None,
        "payload": dict(response.payload),
    }


def classify_intake_ack(dispatch: dict[str, Any]) -> tuple[str, str]:
    """Delivery disposition of one ``emit_graph_inference_result`` result.

    ``accepted`` only on EIE's complete intake ack: a non-failure response whose
    payload says ``status: accepted`` with ``targets_queued`` equal to the
    outputs sent. EIE's handler queues in memory, so even ``accepted`` is an
    intake ack, never consumption. Everything else is not delivered:

    * ``partial``      — accepted with some, not all, targets queued (not retried:
      a resend could duplicate the queued ones);
    * ``unconfirmed``  — a non-failure response without a readable ack;
    * ``blocked``      — Gate is not configured (no direct fallback exists);
    * ``pending``      — Gate/EIE refused, was unreachable, or queued nothing; retriable;
    * ``skipped``      — nothing above the floor, nothing sent.
    """
    status = dispatch.get("status")
    if status == "skipped":
        return "skipped", str(dispatch.get("error", "skipped"))
    if status != "ok":
        error = str(dispatch.get("error") or dispatch.get("packet_type") or "failed")
        return ("blocked" if error == "gate_not_configured" else "pending"), error
    sent = int(dispatch.get("sent_outputs") or 0)
    payload = dispatch.get("payload")
    if not isinstance(payload, dict) or payload.get("status") != "accepted":
        return "unconfirmed", "no_intake_ack"
    queued = payload.get("targets_queued")
    if isinstance(queued, bool) or not isinstance(queued, int) or queued < 0:
        return "unconfirmed", "targets_queued_unreadable"
    if queued == 0 and sent > 0:
        # Nothing was taken in, so resending duplicates nothing: retryable.
        return "pending", f"targets_queued=0 < sent={sent}"
    if queued < sent:
        # Some targets were queued; a blind resend could queue them twice.
        # Terminal until an operator decides (typed, never counted delivered).
        return "partial", f"targets_queued={queued} < sent={sent}"
    if queued > sent:
        return "unconfirmed", f"targets_queued={queued} > sent={sent}"
    return "accepted", f"targets_queued={queued}"


__all__ = [
    "DEFAULT_INFERENCE_TIMEOUT_MS",
    "GRAPH_INFERENCE_ACTION",
    "INFERENCE_CONFIDENCE_FLOOR",
    "build_inference_outputs",
    "classify_intake_ack",
    "emit_graph_inference_result",
    "inference_idempotency_key",
]
