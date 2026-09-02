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

CEG never addresses the enrichment node. It asks Gate to run the `enrich`
action (owned by Enrichment.Inference.Engine in Gate's ownership map) and
receives Gate's response packet. The SDK owns packet construction, signing,
the single HTTP attempt, and the deadline derived from ``timeout_ms``.

Fail-closed rules (seam audit 2026-09-02):
  * no GATE_URL -> ``gate_not_configured``; there is no direct fallback;
  * one attempt per call; retry is the caller's decision and requires the
    idempotency key returned in the result;
  * every SDK error is reported as a typed failure, never swallowed as success.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Sequence
from typing import Any

from constellation_node_sdk import GateClientError

from engine.gate_client import get_gate_client

logger = logging.getLogger(__name__)

ENRICH_ACTION = "enrich"
DEFAULT_ENRICH_TIMEOUT_MS = 25_000
_SEAM_TAGS: tuple[str, ...] = ("INTER_NODE",)


def build_enrichment_request(
    *,
    entity_id: str,
    domain: str,
    target_fields: Sequence[str],
    entity: dict[str, Any] | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    """Shape the payload EIE's `enrich` handler validates (EIE ``EnrichRequest``).

    Keys: ``entity`` (record fields), ``object_type`` (source object name),
    ``schema`` ({field: type}), ``objective`` (natural-language instruction),
    ``kb_context`` (KB profile selector). EIE owns that model; CEG adapts to it.
    """
    fields = [f for f in dict.fromkeys(target_fields) if f]
    if not entity_id or not domain:
        msg = "entity_id and domain are required for an enrichment request"
        raise ValueError(msg)
    if not fields:
        msg = "at least one target field is required for an enrichment request"
        raise ValueError(msg)
    record = {"entity_id": entity_id, "domain": domain, **(entity or {})}
    return {
        "entity": record,
        "object_type": domain,
        "schema": dict.fromkeys(fields, "string"),
        "objective": objective
        or (
            f"Fill {len(fields)} gate-critical field(s) for entity {entity_id} in domain {domain}: {', '.join(fields)}"
        ),
        "kb_context": domain,
    }


def enrichment_idempotency_key(tenant: str, entity_id: str, target_fields: Sequence[str]) -> str:
    digest = hashlib.sha256("|".join([tenant, entity_id, *sorted(set(target_fields))]).encode("utf-8")).hexdigest()
    return f"ceg:enrich:{tenant}:{entity_id}:{digest[:16]}"


async def request_enrichment(
    *,
    tenant: str,
    entity_id: str,
    domain: str,
    target_fields: Sequence[str],
    entity: dict[str, Any] | None = None,
    objective: str | None = None,
    timeout_ms: int = DEFAULT_ENRICH_TIMEOUT_MS,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Ask Gate to run EIE's `enrich` for one entity. One attempt, fail closed."""
    if not os.environ.get("GATE_URL", "").strip():
        logger.warning("gate_egress: GATE_URL unset — enrichment request for %s not sent", entity_id)
        return {"status": "failed", "error": "gate_not_configured", "action": ENRICH_ACTION}

    payload = build_enrichment_request(
        entity_id=entity_id,
        domain=domain,
        target_fields=target_fields,
        entity=entity,
        objective=objective,
    )
    key = enrichment_idempotency_key(tenant, entity_id, target_fields)

    try:
        client = get_gate_client()
        response = await client.execute(
            action=ENRICH_ACTION,
            payload=payload,
            tenant=tenant,
            idempotency_key=key,
            timeout_ms=timeout_ms,
            correlation_id=correlation_id,
            compliance_tags=_SEAM_TAGS,
        )
    except GateClientError as exc:
        logger.warning("gate_egress: %s for entity=%s tenant=%s: %s", type(exc).__name__, entity_id, tenant, exc)
        return {
            "status": "failed",
            "error": type(exc).__name__,
            "detail": str(exc),
            "action": ENRICH_ACTION,
            "idempotency_key": key,
        }

    failed = response.header.packet_type == "failure"
    return {
        "status": "failed" if failed else "ok",
        "action": ENRICH_ACTION,
        "idempotency_key": key,
        "packet_id": str(response.header.packet_id),
        "packet_type": response.header.packet_type,
        "correlation_id": str(response.header.correlation_id) if response.header.correlation_id else None,
        "payload": dict(response.payload),
    }


__all__ = [
    "DEFAULT_ENRICH_TIMEOUT_MS",
    "ENRICH_ACTION",
    "build_enrichment_request",
    "enrichment_idempotency_key",
    "request_enrichment",
]
