"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, inference, feedback, gate]
owner: engine-team
status: active
--- /L9_META ---

A3 (EIE-CEG-GATE-COLLAB-2026-11-21) — the graph inference trigger module,
exercised directly (no handlers): graph-read evidence, change decision,
compare-and-set obligation, delivery disposition. Handler wiring and the
scenario suite through ``sync``/``admin`` live in test_graph_inference_handlers.
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from constellation_node_sdk import GateClientError

from engine.config.schema import DomainSpec
from engine.feedback import graph_inference_trigger as trigger
from engine.gate_egress import build_inference_outputs, classify_intake_ack
from engine.inference_rule_registry import _RULE_REGISTRY, InferenceResult, register_inference_rule
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit
TENANT, LABEL = "cheese-aging", "AgingCave"
SIZE = "infer_company_size_tier"

for _name, _result in {
    "test_a3_low_confidence": InferenceResult("low_field", "x", 0.54, "test_a3_low_confidence"),
    "test_a3_nonfinite": InferenceResult("nan_field", "x", math.nan, "test_a3_nonfinite"),
    "test_a3_malformed_field": InferenceResult("", "x", 0.9, "test_a3_malformed_field"),
}.items():
    if _name not in _RULE_REGISTRY:
        register_inference_rule(_name)(lambda e, c, r=_result: r)


def _spec(rules: list[str], version: str = "0.2.0") -> DomainSpec:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text())
    raw["domain"]["version"] = version
    raw["plugins"] = {"inference_rules": rules, "inference_rule_parameters": {}}
    return DomainSpec.model_validate(raw)


class FakeGraph:
    """Executes the trigger's tagged statements with the semantics of their Cypher."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.obligations: dict[str, dict[str, Any]] = {}
        self.statements: list[str] = []
        self.constraint_present = True

    async def execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None
    ) -> list[Any]:
        p = parameters or {}
        tag = cypher.splitlines()[0]
        self.statements.append(tag)
        if "constraint_check" in tag:
            return [{"name": trigger.OBLIGATION_CONSTRAINT_NAME}] if self.constraint_present else []
        if "read_entities" in tag:
            return [
                {"entity_id": i, "props": n}
                for i, n in self.nodes.items()
                if i in p["ids"] and n.get("_tenant") == p["tenant"]
            ]
        if "read_obligations" in tag:
            return [{"entity_id": i, "obligation": dict(o)} for i, o in self.obligations.items() if i in p["ids"]]
        if "record_evaluations" in tag:
            written = []
            for row in p["rows"]:
                o = self.obligations.setdefault(row["entity_id"], {})
                if (o.get("evidence_revision") or "", o.get("rule_revision") or "") != (
                    row["prior_evidence_revision"] or "",
                    row["prior_rule_revision"] or "",
                ):
                    continue
                emit = row["decision"] == "emit"
                o.update(
                    evidence_revision=row["evidence_revision"],
                    rule_revision=row["rule_revision"],
                    decision=row["decision"],
                    status={"emit": "pending", "same_findings": "accepted"}.get(row["decision"], "no_findings"),
                    pending_key=row["pending_key"] if emit else None,
                    pending_findings_digest=row["findings_digest"] if emit else None,
                    pending_outputs_json=row["outputs_json"] if emit else None,
                )
                written.append({"entity_id": row["entity_id"]})
            return written
        if "record_delivery" in tag:
            o = self.obligations.get(p["entity_id"])
            if o is not None and o.get("pending_key") == p["pending_key"]:
                o["status"] = p["status"]
                if p["status"] == "accepted":
                    o["accepted_findings_digest"] = o.get("pending_findings_digest")
        return []


class FakeGate:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queued: int | None = None
        self.error: Exception | None = None

    async def execute(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        if self.error is not None:
            raise self.error
        n = len(kwargs["payload"]["inference_outputs"])
        response = MagicMock(header=MagicMock(packet_id="pkt", packet_type="response", correlation_id=None))
        response.payload = {"status": "accepted", "targets_queued": n if self.queued is None else self.queued}
        return response


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> FakeGate:
    from engine.state import get_state

    get_state().reset()
    fake = FakeGate()
    monkeypatch.setenv("GATE_URL", "http://gate.test")
    monkeypatch.setattr("engine.gate_egress.get_gate_client", lambda: fake)
    return fake


async def _run(graph: FakeGraph, spec: DomainSpec, rules: list[str] | None = None) -> trigger.TriggerReport:
    report = await trigger.evaluate_and_record(
        graph, spec, tenant=TENANT, label=LABEL, id_property="cave_id", ids=list(graph.nodes), rules=rules
    )
    trigger.schedule_deliveries(graph, spec, tenant=TENANT, label=LABEL, report=report)
    await trigger.drain_inflight()
    return report


def _node(graph: FakeGraph, **props: Any) -> None:
    graph.nodes["c1"] = {"cave_id": "c1", "_tenant": TENANT, **props}


@pytest.mark.parametrize(
    ("rule", "props", "reason"),
    [
        ("test_a3_low_confidence", {}, "no_confident_inference"),
        ("test_a3_nonfinite", {}, "nonfinite_confidence"),
        ("test_a3_malformed_field", {}, "malformed_field"),
        (SIZE, {"employee_count": 40, "company_size_tier": "small"}, "already_in_graph"),
    ],
)
def test_unsendable_findings_are_skipped_with_a_reason(rule: str, props: dict[str, Any], reason: str) -> None:
    ev = trigger.evaluate_entity(tenant=TENANT, domain_spec=_spec([rule]), entity_id="c1", props=props, rules=[rule])
    assert ev.findings == []
    assert ev.skipped == [{"rule": rule, "reason": reason}]


@pytest.mark.asyncio
async def test_changed_fact_sends_one_cited_finding_then_unchanged_sends_nothing(gate: FakeGate) -> None:
    graph, spec = FakeGraph(), _spec([SIZE])
    _node(graph, employee_count=40, updated_at="t1")
    await _run(graph, spec)
    [output] = gate.sent[0]["payload"]["inference_outputs"]
    assert (output["field"], output["value"], output["rule"]) == ("company_size_tier", "small", SIZE)
    assert output["provenance"].startswith(f"ceg:graph:{LABEL}:c1@")
    assert gate.sent[0]["idempotency_key"] == graph.obligations["c1"]["pending_key"]
    assert graph.obligations["c1"]["status"] == "accepted"
    graph.nodes["c1"]["updated_at"] = "t2"  # timestamps are not evidence
    graph.statements.clear()
    await _run(graph, spec)
    assert len(gate.sent) == 1
    assert "// ceg:inference:record_evaluations" not in graph.statements


@pytest.mark.asyncio
async def test_real_change_resends_and_a_return_to_the_accepted_value_retires_pending(gate: FakeGate) -> None:
    graph, spec = FakeGraph(), _spec([SIZE])
    _node(graph, employee_count=40)
    await _run(graph, spec)  # small accepted
    gate.error = GateClientError("down")
    graph.nodes["c1"]["employee_count"] = 120
    await _run(graph, spec)  # mid_market pending
    stale = trigger.PendingDelivery(
        "c1", graph.obligations["c1"]["pending_key"], json.loads(graph.obligations["c1"]["pending_outputs_json"])
    )
    assert stale.pending_key != gate.sent[0]["idempotency_key"]
    gate.error = None
    graph.nodes["c1"]["employee_count"] = 40
    report = await _run(graph, spec)
    assert report.decisions == {"same_findings": 1}
    assert (graph.obligations["c1"]["status"], graph.obligations["c1"]["pending_key"]) == ("accepted", None)
    sent = len(gate.sent)
    assert await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1") is None
    assert (await trigger.deliver(graph, spec, tenant=TENANT, label=LABEL, pending=stale))[
        "delivery_status"
    ] == "superseded"
    assert len(gate.sent) == sent


@pytest.mark.asyncio
async def test_older_overlapping_evaluation_cannot_replace_newer_state(gate: FakeGate) -> None:
    graph, spec = FakeGraph(), _spec([SIZE])
    _node(graph, employee_count=40)
    await _run(graph, spec)
    newer = dict(graph.obligations["c1"])
    row = dict.fromkeys(("prior_evidence_revision", "prior_rule_revision"))
    row |= {
        "entity_id": "c1",
        "evidence_revision": "old",
        "rule_revision": "old",
        "decision": "emit",
        "findings_digest": "old",
        "pending_key": "old",
        "outputs_json": "[]",
    }
    assert await trigger.record_evaluations(graph, spec, tenant=TENANT, label=LABEL, rows=[row]) == set()
    assert graph.obligations["c1"] == newer


@pytest.mark.asyncio
async def test_stale_rule_revision_reevaluates_without_resending(gate: FakeGate) -> None:
    graph = FakeGraph()
    _node(graph, employee_count=40)
    await _run(graph, _spec([SIZE]))
    assert (await _run(graph, _spec([SIZE], version="0.3.0"))).decisions == {"same_findings": 1}
    assert len(gate.sent) == 1


@pytest.mark.asyncio
async def test_rule_selection_is_bounded_by_the_domain(gate: FakeGate) -> None:
    graph, spec = FakeGraph(), _spec([SIZE])
    _node(graph, employee_count=40, website="https://www.example.org")
    assert (await _run(graph, spec, rules=[])).evaluated == 0
    await _run(graph, spec, rules=["infer_email_domain_from_website", SIZE])  # unauthorized name ignored
    assert [o["field"] for o in gate.sent[0]["payload"]["inference_outputs"]] == ["company_size_tier"]


@pytest.mark.asyncio
async def test_other_tenants_node_and_missing_constraint_write_nothing(gate: FakeGate) -> None:
    from engine.state import get_state

    graph, spec = FakeGraph(), _spec([SIZE])
    graph.nodes["c1"] = {"cave_id": "c1", "employee_count": 40, "_tenant": "someone-else"}
    assert (await _run(graph, spec)).decisions == {"entity_not_found_for_tenant": 1}
    _node(graph, employee_count=40)
    graph.constraint_present = False
    get_state().inference_constraint_databases.clear()
    assert (await _run(graph, spec)).decisions == {"obligation_constraint_missing": 1}
    assert graph.obligations == {}
    assert gate.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("queued", "status"), [(0, "pending"), (None, "accepted")])
async def test_zero_intake_is_retryable_under_the_same_key(gate: FakeGate, queued: int | None, status: str) -> None:
    graph, spec = FakeGraph(), _spec([SIZE])
    _node(graph, employee_count=40)
    gate.queued = 0
    await _run(graph, spec)
    assert graph.obligations["c1"]["status"] == "pending"
    gate.queued = queued
    await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1")
    assert graph.obligations["c1"]["status"] == status
    assert gate.sent[0]["idempotency_key"] == gate.sent[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_partial_intake_is_never_counted_or_blindly_resent(gate: FakeGate) -> None:
    graph, spec = FakeGraph(), _spec([SIZE, "infer_email_domain_from_website"])
    _node(graph, employee_count=40, website="https://www.example.org")
    gate.queued = 1
    await _run(graph, spec)
    assert graph.obligations["c1"]["status"] == "partial"
    assert "accepted_findings_digest" not in graph.obligations["c1"]
    assert await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1") is None


@pytest.mark.asyncio
async def test_gate_not_configured_is_blocked(gate: FakeGate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATE_URL")
    graph = FakeGraph()
    _node(graph, employee_count=40)
    await _run(graph, _spec([SIZE]))
    assert graph.obligations["c1"]["status"] == "blocked"
    assert gate.sent == []


@pytest.mark.parametrize(
    ("queued", "sent", "status"),
    [
        (2, 2, "accepted"),
        (1, 2, "partial"),
        (0, 2, "pending"),
        (3, 2, "unconfirmed"),
        (True, 2, "unconfirmed"),
        (None, 2, "unconfirmed"),
    ],
)
def test_intake_ack_classification(queued: Any, sent: int, status: str) -> None:
    payload = {"status": "accepted"} | ({} if queued is None else {"targets_queued": queued})
    assert classify_intake_ack({"status": "ok", "sent_outputs": sent, "payload": payload})[0] == status
    assert classify_intake_ack({"status": "failed", "error": "gate_not_configured"})[0] == "blocked"


@pytest.mark.parametrize("confidence", [math.nan, math.inf, 1.5])
def test_egress_drops_out_of_range_confidence(confidence: float) -> None:
    assert build_inference_outputs("e-1", [{"field": "f", "value": 1, "confidence": confidence}]) == []


def test_obligation_key_is_unique_on_tenant_type_and_id() -> None:
    assert "REQUIRE (o.tenant, o.entity_type, o.entity_id) IS UNIQUE" in trigger.obligation_constraint_cypher()
