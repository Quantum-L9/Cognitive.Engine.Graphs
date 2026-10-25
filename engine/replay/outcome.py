"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [replay]
tags: [replay, outcome, offline, task-033]
owner: engine-team
status: active
--- /L9_META ---

Offline, deterministic outcome replay over recorded transport events.

The replay core is generic: it accepts a document of recorded events
(``tenant``, ``action``, ``packet_id`` plus optional provenance fields),
normalizes them and derives deterministic outcome hashes without network or
Gate mutation. Which producers may feed it is a registry of accepted input
schema ids. The Odoo exporter's ``l9.odoo.outcome_replay_input.v1``
(IB-Odoo_19 ADR-126, ``scripts/outcome_replay/schema.py``) is the one
registered producer today; it is an adapter entry, not the engine's identity
(Stage 2, R10).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ODOO_INPUT_SCHEMA = "l9.odoo.outcome_replay_input.v1"
# Input schema ids this replay accepts. Each is a producer-owned contract
# verified against the producer repository before registration.
ACCEPTED_INPUT_SCHEMAS: frozenset[str] = frozenset({ODOO_INPUT_SCHEMA})
REPLAY_OUTCOME_SCHEMA = "l9.ceg.outcome_replay.v1"
REQUIRED_EVENT_FIELDS = ("tenant", "action", "packet_id")


class ReplayError(ValueError):
    """Invalid replay input or forbidden live-path usage."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def load_replay_input(document: dict[str, Any]) -> dict[str, Any]:
    """Validate a recorded-event replay document; never mutates Gate or opens network."""
    if not isinstance(document, dict):
        raise ReplayError("replay input must be an object")
    schema_id = document.get("schema")
    if schema_id not in ACCEPTED_INPUT_SCHEMAS:
        raise ReplayError(f"unsupported schema: {schema_id!r} (accepted: {sorted(ACCEPTED_INPUT_SCHEMAS)})")
    if document.get("gate_mutation") is True:
        raise ReplayError("gate_mutation=true is forbidden in replay mode")
    events = document.get("events")
    if not isinstance(events, list):
        raise ReplayError("events must be a list")
    normalized: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict):
            raise ReplayError("each event must be an object")
        missing = [f for f in REQUIRED_EVENT_FIELDS if not str(raw.get(f) or "").strip()]
        if missing:
            raise ReplayError(f"event missing required fields: {missing}")
        event = {
            "action": str(raw["action"]).strip(),
            "packet_id": str(raw["packet_id"]).strip(),
            "tenant": str(raw["tenant"]).strip(),
        }
        for optional in ("correlation_id", "source_model", "source_id", "observed_at"):
            if raw.get(optional) is not None:
                event[optional] = str(raw[optional])
        if isinstance(raw.get("payload"), dict):
            event["payload"] = raw["payload"]
        normalized.append(event)
    normalized.sort(key=lambda e: (e["packet_id"], e["action"], e["tenant"]))
    return {
        "schema": schema_id,
        "schema_version": str(document.get("schema_version") or ""),
        "producer": document.get("producer"),
        "producer_task": document.get("producer_task"),
        "gate_mutation": False,
        "event_count": len(normalized),
        "events": normalized,
        "content_hash": document.get("content_hash"),
    }


def replay_outcomes(document: dict[str, Any]) -> dict[str, Any]:
    """Derive deterministic outcome hashes from recorded events (no I/O)."""
    loaded = load_replay_input(document)
    outcomes: list[dict[str, Any]] = []
    for event in loaded["events"]:
        body = {
            "action": event["action"],
            "packet_id": event["packet_id"],
            "payload": event.get("payload", {}),
            "tenant": event["tenant"],
        }
        for optional in ("correlation_id", "source_model", "source_id", "observed_at"):
            if optional in event:
                body[optional] = event[optional]
        outcomes.append(
            {
                "action": event["action"],
                "outcome_hash": _digest(body),
                "packet_id": event["packet_id"],
                "tenant": event["tenant"],
            }
        )
    result: dict[str, Any] = {
        "event_count": len(outcomes),
        "gate_calls": 0,
        "input_content_hash": loaded.get("content_hash"),
        "input_schema": loaded["schema"],
        "network": False,
        "outcomes": outcomes,
        "replay_mode": True,
        "schema": REPLAY_OUTCOME_SCHEMA,
    }
    result["outcome_set_hash"] = _digest({"outcomes": outcomes, "schema": REPLAY_OUTCOME_SCHEMA})
    return result
