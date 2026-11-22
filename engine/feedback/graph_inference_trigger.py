"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [feedback]
tags: [inference, feedback, gate, eie, obligation]
owner: engine-team
status: active
--- /L9_META ---

engine/feedback/graph_inference_trigger.py — graph-derived inference, read from
the graph, recorded as a durable obligation, delivered to EIE after the write.

The admin ``emit_inference_feedback`` subaction ran the inference rules over an
``entity`` dict the caller sent, so the "graph inference" CEG handed EIE was
whatever the caller asserted (A3). This module replaces that input with an
authoritative, tenant-scoped read of the node as the graph holds it, and adds
the one opt-in trigger: a node ``sync`` whose write committed.

Lifecycle of one entity (states are this module's, not EIE's):

1. **evaluate** — read the node (``_tenant`` must equal the request tenant),
   compute ``evidence_revision`` over its first-party properties and
   ``rule_revision`` over the domain-authorized rules and their declared
   parameters, run only those rules.
2. **decide** — compare with the entity's ``CegInferenceObligation`` record:
   an unchanged evidence + rule revision is not new evidence (``unchanged``);
   a finding whose value the graph already holds proposes nothing (EIE echo,
   copied observation); a finding set equal to the last *accepted* one is not
   resent (``same_findings``). Only a changed, non-empty finding set becomes a
   ``pending`` obligation with a stable idempotency key.
3. **deliver** — after the source write committed and outside any open graph
   transaction: one ``graph-inference-result`` through ``engine.gate_egress``.
   The obligation becomes ``accepted`` only on EIE's complete intake ack
   (``status=accepted`` and ``targets_queued`` equal to the outputs sent).
   ``partial``, ``unconfirmed``, ``blocked`` and ``pending`` are never counted as
   delivered. Accepted is an intake ack, not consumption: EIE's queue is not a
   durable handling record.

The obligation lives in the tenant's own graph database (the existing CEG
store), one node per (tenant, entity_type, entity_id), where entity_type is the
entity's node label (two sync endpoints of one label share one obligation), so dedup survives a
restart and a failed delivery stays retriable. There is no second bus.

Activation: ``settings.graph_inference_trigger_enabled`` (default False) AND
``settings.graph_inference_feedback_enabled`` (the egress gate) AND a non-empty
``plugins.inference_rules`` in the tenant's own spec. Nothing here runs a rule
a domain did not authorize, and ``rules=[]`` means none.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.inference_rule_registry import (
    InferenceContext,
    InferenceResult,
    authorized_rules,
    execute_rule,
)
from engine.utils.security import sanitize_label

logger = logging.getLogger(__name__)

OBLIGATION_LABEL = "CegInferenceObligation"
OBLIGATION_CONSTRAINT_NAME = "ceg_inference_obligation_key"
# Bounded: one trigger evaluates at most this many entities from one sync batch.
MAX_TRIGGER_ENTITIES = 500
# Bounded: background deliveries alive at once; beyond it obligations stay pending.
MAX_INFLIGHT_DELIVERIES = 64

# Properties that move on every write and say nothing about the entity.
_VOLATILE_PROPERTIES = frozenset({"updated_at", "created_at"})

# Not-accepted statuses a retry may re-send (gate_egress.classify_intake_ack names them all).
STATUS_PENDING = "pending"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_BLOCKED = "blocked"

# Decisions.
DECISION_UNCHANGED = "unchanged"
DECISION_NO_FINDINGS = "no_findings"
DECISION_SAME_FINDINGS = "same_findings"
DECISION_EMIT = "emit"
DECISION_NOT_FOUND = "entity_not_found_for_tenant"
DECISION_SUPERSEDED = "superseded_by_concurrent_evaluation"
DECISION_NO_CONSTRAINT = "obligation_constraint_missing"


def obligation_constraint_cypher() -> str:
    """Uniqueness of the obligation key; ``entity_type`` holds the entity's node label."""
    return (
        f"CREATE CONSTRAINT ceg_inference_obligation_key IF NOT EXISTS FOR (o:{sanitize_label(OBLIGATION_LABEL)}) "
        "REQUIRE (o.tenant, o.entity_type, o.entity_id) IS UNIQUE"
    )


def trigger_active(settings: Any, domain_spec: Any) -> bool:
    """All three activation boundaries: trigger flag, egress flag, domain opt-in."""
    return bool(
        getattr(settings, "graph_inference_trigger_enabled", False)
        and getattr(settings, "graph_inference_feedback_enabled", False)
        and authorized_rules(domain_spec)
    )


# ── Revisions ────────────────────────────────────────────────────────────────


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def first_party_properties(props: Mapping[str, Any]) -> dict[str, Any]:
    """The node's own facts: no CEG-internal (``_``-prefixed) or timestamp keys."""
    return {k: v for k, v in props.items() if not k.startswith("_") and k not in _VOLATILE_PROPERTIES}


def evidence_revision(props: Mapping[str, Any]) -> str:
    """Revision of the graph evidence a rule reads: digest of first-party properties."""
    return _digest(first_party_properties(props))


def rule_revision(domain_spec: Any, rules: Sequence[str]) -> str:
    """Revision of what may run: the rule names, their declared parameters, the spec version."""
    plugins = getattr(domain_spec, "plugins", None)
    params = dict(getattr(plugins, "inference_rule_parameters", None) or {}) if plugins is not None else {}
    domain = getattr(domain_spec, "domain", None)
    return _digest(
        {
            "rules": sorted(rules),
            "parameters": {name: params.get(name) for name in sorted(rules)},
            "spec_version": getattr(domain, "version", None),
        }
    )


# ── Evaluation ───────────────────────────────────────────────────────────────


@dataclass
class Evaluation:
    """One entity's inference over its graph state, before the change decision."""

    entity_id: str
    evidence_revision: str
    rule_revision: str
    findings: list[InferenceResult] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def findings_digest(self) -> str:
        return findings_digest(self.findings)


def findings_digest(findings: Iterable[InferenceResult]) -> str:
    return _digest(
        sorted(
            [f.rule_name, f.field_name, json.dumps(f.value, sort_keys=True, default=str), round(f.confidence, 6)]
            for f in findings
        )
    )


def _finding_defect(result: InferenceResult, floor: float) -> str | None:
    if not isinstance(result.field_name, str) or not result.field_name:
        return "malformed_field"
    try:
        confidence = float(result.confidence)
    except (TypeError, ValueError):
        return "nonnumeric_confidence"
    if not math.isfinite(confidence):
        return "nonfinite_confidence"
    if confidence < floor or confidence > 1.0:
        return "confidence_out_of_range" if confidence > 1.0 else "below_confidence_floor"
    return None


def evaluate_entity(
    *,
    tenant: str,
    domain_spec: Any,
    entity_id: str,
    props: Mapping[str, Any],
    rules: Sequence[str],
) -> Evaluation:
    """Run the authorized ``rules`` over the node's graph properties.

    ``rules`` must already be a subset of ``authorized_rules(domain_spec)``; the
    caller enforces that, this function never widens it. A finding whose value
    the graph already holds is skipped: re-proposing an existing fact (EIE's own
    writeback, a copied observation) is not new evidence.
    """
    plugins = getattr(domain_spec, "plugins", None)
    entity = first_party_properties(props)
    context = InferenceContext(
        tenant_id=tenant,
        domain_id=str(domain_spec.domain.id),
        pass_number=1,
        known_fields=dict(entity),
        rule_parameters=dict(plugins.inference_rule_parameters) if plugins else {},
    )
    evaluation = Evaluation(
        entity_id=entity_id,
        evidence_revision=evidence_revision(props),
        rule_revision=rule_revision(domain_spec, rules),
    )
    for rule_name in rules:
        result = execute_rule(rule_name, dict(entity), context)
        if result is None:
            evaluation.skipped.append({"rule": rule_name, "reason": "no_confident_inference"})
            continue
        defect = _finding_defect(result, context.confidence_floor)
        if defect is not None:
            evaluation.skipped.append({"rule": rule_name, "reason": defect})
            continue
        if result.field_name in entity and entity[result.field_name] == result.value:
            evaluation.skipped.append({"rule": rule_name, "reason": "already_in_graph"})
            continue
        evaluation.findings.append(result)
    return evaluation


# ── Change decision ──────────────────────────────────────────────────────────


def obligation_key(tenant: str, entity_ref: str, evaluation: Evaluation) -> str:
    """Binds tenant, entity, evidence revision, rule revision and the finding set."""
    digest = _digest(
        [tenant, entity_ref, evaluation.evidence_revision, evaluation.rule_revision, evaluation.findings_digest]
    )
    return f"ceg:graph-inference:{tenant}:{entity_ref}:{digest[:24]}"


def decide(prior: Mapping[str, Any] | None, evaluation: Evaluation) -> str:
    """Whether this evaluation is new consequential work."""
    if (
        prior is not None
        and prior.get("evidence_revision") == evaluation.evidence_revision
        and prior.get("rule_revision") == evaluation.rule_revision
    ):
        return DECISION_UNCHANGED
    if not evaluation.findings:
        return DECISION_NO_FINDINGS
    if prior is not None and prior.get("accepted_findings_digest") == evaluation.findings_digest:
        return DECISION_SAME_FINDINGS
    return DECISION_EMIT


# ── Graph IO (GraphDriver.execute_query only) ────────────────────────────────


def _pattern(var: str, label: str, props: str = "") -> str:
    """``(var:Label props)`` with the label sanitized; ``props`` holds ``$param`` maps only."""
    body = f"{var}:{sanitize_label(label)}"
    return "(" + body + (" " + props if props else "") + ")"


def _database(domain_spec: Any) -> str:
    return str(domain_spec.domain.id)


async def read_entities(
    driver: Any, domain_spec: Any, *, tenant: str, label: str, id_property: str, ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Authoritative tenant-scoped read: a node owned by another tenant is not found."""
    cypher = (
        "// ceg:inference:read_entities\n"
        "MATCH "
        + _pattern("n", label)
        + f" WHERE n.{sanitize_label(id_property)} IN $ids AND n._tenant = $tenant\n"
        + f"RETURN toString(n.{sanitize_label(id_property)}) AS entity_id, properties(n) AS props"
    )
    rows = await driver.execute_query(
        cypher=cypher, parameters={"ids": list(ids), "tenant": tenant}, database=_database(domain_spec)
    )
    return {str(r["entity_id"]): dict(r["props"]) for r in rows or [] if isinstance(r, Mapping)}


async def read_obligations(
    driver: Any, domain_spec: Any, *, tenant: str, label: str, ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    cypher = (
        "// ceg:inference:read_obligations\n"
        "MATCH "
        + _pattern("o", OBLIGATION_LABEL)
        + " WHERE o.tenant = $tenant AND o.entity_type = $label AND o.entity_id IN $ids\n"
        + "RETURN o.entity_id AS entity_id, properties(o) AS obligation"
    )
    rows = await driver.execute_query(
        cypher=cypher,
        parameters={"tenant": tenant, "label": label, "ids": list(ids)},
        database=_database(domain_spec),
    )
    return {str(r["entity_id"]): dict(r["obligation"]) for r in rows or [] if isinstance(r, Mapping)}


async def record_evaluations(
    driver: Any, domain_spec: Any, *, tenant: str, label: str, rows: list[dict[str, Any]]
) -> set[str]:
    """Persist each evaluation; return the entity ids whose write applied.

    Compare-and-set: a row applies only while the stored revisions still equal
    the ones its decision read (``prior_*``), so an overlapping evaluation that
    read older evidence cannot replace newer state. The node is write-locked
    (``_ceg_lock`` written and removed: the lock stays for the transaction)
    before the comparison. ``same_findings`` and
    ``no_findings`` retire any pending work they supersede; accepted history
    (``accepted_*``) is kept.
    """
    if not rows:
        return set()
    cypher = (
        "// ceg:inference:record_evaluations\n"
        "UNWIND $rows AS row\n"
        "MERGE "
        + _pattern("o", OBLIGATION_LABEL, "{tenant: $tenant, entity_type: $label, entity_id: row.entity_id}")
        + "\n"
        "SET o._ceg_lock = true REMOVE o._ceg_lock\n"
        "WITH o, row\n"
        "WHERE coalesce(o.evidence_revision, '') = coalesce(row.prior_evidence_revision, '')\n"
        "  AND coalesce(o.rule_revision, '') = coalesce(row.prior_rule_revision, '')\n"
        "WITH o, row, row.decision = 'emit' AS emit\n"
        "SET o.evidence_revision = row.evidence_revision, o.rule_revision = row.rule_revision,\n"
        "    o.decision = row.decision, o.evaluated_at = datetime(),\n"
        "    o.status = CASE row.decision WHEN 'emit' THEN 'pending'\n"
        "                    WHEN 'same_findings' THEN 'accepted' ELSE 'no_findings' END,\n"
        "    o.pending_key = CASE WHEN emit THEN row.pending_key ELSE null END,\n"
        "    o.pending_findings_digest = CASE WHEN emit THEN row.findings_digest ELSE null END,\n"
        "    o.pending_outputs_json = CASE WHEN emit THEN row.outputs_json ELSE null END,\n"
        "    o.attempts = CASE WHEN emit THEN 0 ELSE coalesce(o.attempts, 0) END\n"
        "RETURN row.entity_id AS entity_id"
    )
    written = await driver.execute_query(
        cypher=cypher,
        parameters={"tenant": tenant, "label": label, "rows": rows},
        database=_database(domain_spec),
    )
    return {str(r["entity_id"]) for r in written or [] if isinstance(r, Mapping)}


_VERIFIED_CONSTRAINT_DATABASES: set[str] = set()  # bounded: one entry per tenant database


async def obligation_constraint_present(driver: Any, domain_spec: Any) -> bool:
    """Whether the obligation-key uniqueness constraint exists in the tenant database.

    Without it concurrent MERGEs may create two obligation nodes for one entity,
    so the trigger writes nothing until ``init_schema`` has created it. A
    positive answer is cached per database for the process.
    """
    database = _database(domain_spec)
    if database in _VERIFIED_CONSTRAINT_DATABASES:
        return True
    rows = await driver.execute_query(
        cypher="// ceg:inference:constraint_check\nSHOW CONSTRAINTS YIELD name RETURN name",
        parameters={},
        database=database,
    )
    present = any(isinstance(r, Mapping) and r.get("name") == OBLIGATION_CONSTRAINT_NAME for r in rows or [])
    if present:
        _VERIFIED_CONSTRAINT_DATABASES.add(database)
    return present


async def record_delivery(
    driver: Any,
    domain_spec: Any,
    *,
    tenant: str,
    label: str,
    entity_id: str,
    pending_key: str,
    status: str,
    detail: str,
) -> None:
    """Record a delivery disposition, only against the obligation it was for.

    A delivery for a key that is no longer the pending one (a newer change was
    recorded meanwhile) does not overwrite the newer obligation.
    """
    cypher = (
        "// ceg:inference:record_delivery\n"
        "MATCH "
        + _pattern("o", OBLIGATION_LABEL, "{tenant: $tenant, entity_type: $label, entity_id: $entity_id}")
        + "\n"
        + "WHERE o.pending_key = $pending_key\n"
        "SET o.status = $status, o.last_detail = $detail, o.attempts = coalesce(o.attempts, 0) + 1,\n"
        "    o.delivered_at = CASE WHEN $status = 'accepted' THEN datetime() ELSE o.delivered_at END,\n"
        "    o.accepted_key = CASE WHEN $status = 'accepted' THEN $pending_key ELSE o.accepted_key END,\n"
        "    o.accepted_findings_digest = CASE WHEN $status = 'accepted' THEN o.pending_findings_digest\n"
        "                                      ELSE o.accepted_findings_digest END"
    )
    await driver.execute_query(
        cypher=cypher,
        parameters={
            "tenant": tenant,
            "label": label,
            "entity_id": entity_id,
            "pending_key": pending_key,
            "status": status,
            "detail": detail,
        },
        database=_database(domain_spec),
    )


# ── Orchestration ────────────────────────────────────────────────────────────


@dataclass
class PendingDelivery:
    entity_id: str
    pending_key: str
    outputs: list[dict[str, Any]]


@dataclass
class TriggerReport:
    evaluated: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    pending: list[PendingDelivery] = field(default_factory=list)
    skipped: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def count(self, decision: str) -> None:
        self.decisions[decision] = self.decisions.get(decision, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "decisions": dict(self.decisions),
            "pending": [p.pending_key for p in self.pending],
            "skipped": {k: list(v) for k, v in self.skipped.items()},
        }


async def evaluate_and_record(
    driver: Any,
    domain_spec: Any,
    *,
    tenant: str,
    label: str,
    id_property: str,
    ids: Sequence[Any],
    rules: Sequence[str] | None = None,
) -> TriggerReport:
    """Read, evaluate, decide and persist. Sends nothing.

    ``rules`` None means every rule the domain authorizes; an explicit list must
    be a subset (the caller validates) and ``[]`` runs none.
    """
    from engine.gate_egress import build_inference_outputs

    allowed = authorized_rules(domain_spec)
    to_run = list(allowed) if rules is None else [r for r in rules if r in allowed]
    report = TriggerReport()
    unique_ids = list(dict.fromkeys(str(i) for i in ids if i is not None and str(i) != ""))[:MAX_TRIGGER_ENTITIES]
    if not unique_ids or not to_run:
        return report
    if not await obligation_constraint_present(driver, domain_spec):
        logger.warning("graph inference: obligation constraint missing; run admin init_schema")
        report.decisions[DECISION_NO_CONSTRAINT] = len(unique_ids)
        return report

    entities = await read_entities(
        driver, domain_spec, tenant=tenant, label=label, id_property=id_property, ids=unique_ids
    )
    priors = await read_obligations(driver, domain_spec, tenant=tenant, label=label, ids=list(entities))
    entity_ref_prefix = sanitize_label(label)
    rows: list[dict[str, Any]] = []
    for entity_id in unique_ids:
        props = entities.get(entity_id)
        if props is None:
            report.count(DECISION_NOT_FOUND)
            continue
        report.evaluated += 1
        evaluation = evaluate_entity(
            tenant=tenant, domain_spec=domain_spec, entity_id=entity_id, props=props, rules=to_run
        )
        if evaluation.skipped:
            report.skipped[entity_id] = evaluation.skipped
        decision = decide(priors.get(entity_id), evaluation)
        report.count(decision)
        if decision == DECISION_UNCHANGED:
            continue
        prior = priors.get(entity_id) or {}
        row: dict[str, Any] = {
            "entity_id": entity_id,
            "prior_evidence_revision": prior.get("evidence_revision"),
            "prior_rule_revision": prior.get("rule_revision"),
            "evidence_revision": evaluation.evidence_revision,
            "rule_revision": evaluation.rule_revision,
            "decision": decision,
            "findings_digest": evaluation.findings_digest,
            "pending_key": None,
            "outputs_json": None,
        }
        if decision == DECISION_EMIT:
            outputs = build_inference_outputs(entity_id, evaluation.findings)
            for output in outputs:
                output["provenance"] = (
                    f"ceg:graph:{entity_ref_prefix}:{entity_id}@{evaluation.evidence_revision[:16]}"
                    f"/rules@{evaluation.rule_revision[:16]}"
                )
            key = obligation_key(tenant, f"{entity_ref_prefix}:{entity_id}", evaluation)
            row["pending_key"] = key
            row["outputs_json"] = json.dumps(outputs, sort_keys=True, default=str)
            report.pending.append(PendingDelivery(entity_id=entity_id, pending_key=key, outputs=outputs))
        rows.append(row)
    written = await record_evaluations(driver, domain_spec, tenant=tenant, label=label, rows=rows)
    for row in rows:
        if row["entity_id"] not in written:
            report.decisions[row["decision"]] -= 1
            report.count(DECISION_SUPERSEDED)
    report.decisions = {k: v for k, v in report.decisions.items() if v}
    report.pending = [p for p in report.pending if p.entity_id in written]
    return report


async def deliver(
    driver: Any, domain_spec: Any, *, tenant: str, label: str, pending: PendingDelivery
) -> dict[str, Any]:
    """One delivery attempt; the disposition is recorded, never assumed."""
    from engine.gate_egress import classify_intake_ack, emit_graph_inference_result

    current = (await read_obligations(driver, domain_spec, tenant=tenant, label=label, ids=[pending.entity_id])).get(
        pending.entity_id
    )
    if current is None or current.get("pending_key") != pending.pending_key:
        # A later evaluation superseded or retired this obligation: sending it
        # would deliver a finding the graph no longer supports.
        return {
            "status": "skipped",
            "error": "superseded",
            "idempotency_key": pending.pending_key,
            "sent_outputs": 0,
            "delivery_status": "superseded",
            "delivery_detail": "pending_key is no longer the active obligation",
        }
    dispatch = await emit_graph_inference_result(
        tenant=tenant,
        entity_id=pending.entity_id,
        results=pending.outputs,
        idempotency_key=pending.pending_key,
    )
    status, detail = classify_intake_ack(dispatch)
    try:
        await record_delivery(
            driver,
            domain_spec,
            tenant=tenant,
            label=label,
            entity_id=pending.entity_id,
            pending_key=pending.pending_key,
            status=status,
            detail=detail,
        )
    except Exception:
        # The send happened; the record did not. The obligation stays pending and
        # a retry reuses the same idempotency key, so Gate/EIE dedup it.
        logger.warning("graph inference: delivery disposition not recorded for %s", pending.pending_key, exc_info=True)
    return {**dispatch, "delivery_status": status, "delivery_detail": detail}


_INFLIGHT: set[asyncio.Task[Any]] = set()


def schedule_deliveries(driver: Any, domain_spec: Any, *, tenant: str, label: str, report: TriggerReport) -> int:
    """Deliver after the caller returns: never awaited inside the ``sync`` that caused it.

    Returns how many deliveries were scheduled; any beyond the in-flight bound
    stay ``pending`` in the graph for a later retry.
    """
    scheduled = 0
    for pending in report.pending:
        if len(_INFLIGHT) >= MAX_INFLIGHT_DELIVERIES:
            logger.warning("graph inference: in-flight bound reached; %s stays pending", pending.pending_key)
            continue
        task = asyncio.get_running_loop().create_task(
            deliver(driver, domain_spec, tenant=tenant, label=label, pending=pending)
        )
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
        scheduled += 1
    return scheduled


async def drain_inflight() -> None:
    """Await every scheduled delivery (tests and orderly shutdown)."""
    while _INFLIGHT:
        await asyncio.gather(*list(_INFLIGHT), return_exceptions=True)


async def retry_pending(
    driver: Any, domain_spec: Any, *, tenant: str, label: str, entity_id: str
) -> dict[str, Any] | None:
    """Re-deliver a recorded, not-accepted obligation under its original key."""
    priors = await read_obligations(driver, domain_spec, tenant=tenant, label=label, ids=[entity_id])
    prior = priors.get(entity_id)
    if prior is None or prior.get("status") not in (STATUS_PENDING, STATUS_BLOCKED, STATUS_UNCONFIRMED):
        return None
    key = prior.get("pending_key")
    outputs_json = prior.get("pending_outputs_json")
    if not key or not outputs_json:
        return None
    outputs = json.loads(outputs_json)
    return await deliver(
        driver,
        domain_spec,
        tenant=tenant,
        label=label,
        pending=PendingDelivery(entity_id=entity_id, pending_key=str(key), outputs=list(outputs)),
    )


__all__ = [
    "MAX_INFLIGHT_DELIVERIES",
    "MAX_TRIGGER_ENTITIES",
    "OBLIGATION_CONSTRAINT_NAME",
    "OBLIGATION_LABEL",
    "Evaluation",
    "PendingDelivery",
    "TriggerReport",
    "decide",
    "deliver",
    "drain_inflight",
    "evaluate_and_record",
    "evaluate_entity",
    "evidence_revision",
    "findings_digest",
    "first_party_properties",
    "obligation_constraint_cypher",
    "obligation_constraint_present",
    "obligation_key",
    "retry_pending",
    "rule_revision",
    "schedule_deliveries",
    "trigger_active",
]
