"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, inference, feedback, gate, handlers]
owner: engine-team
status: active
--- /L9_META ---

A3b (EIE-CEG-GATE-COLLAB-2026-11-21) — the graph inference trigger wired into
``sync`` (post-commit), admin ``emit_inference_feedback`` and ``init_schema``.
Module-level behaviour is pinned in test_graph_inference_trigger.

Acceptance cases covered at unit level (P22, P23, P24, P28; P05 by the second
synthetic tenant; P03 by the egress seam). The graph store is an in-memory fake
that executes the trigger's tagged statements with the same semantics as their
Cypher; a real-Neo4j run of those statements belongs to integration.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from constellation_node_sdk import GateClientError

from engine.config.schema import DomainSpec
from engine.feedback import graph_inference_trigger as trigger
from engine.gate_egress import GRAPH_INFERENCE_ACTION
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT, synthetic_loader

pytestmark = pytest.mark.unit

TENANT = "cheese-aging"
LABEL = "AgingCave"


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _spec(rules: list[str] | None, *, version: str = "0.2.0", params: dict[str, Any] | None = None) -> DomainSpec:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / "cheese-aging" / "spec.yaml").read_text())
    raw["domain"]["version"] = version
    for node in raw["ontology"]["nodes"]:
        if node["label"] == LABEL:
            node["properties"] += [
                {"name": "employee_count", "type": "int"},
                {"name": "company_size_tier", "type": "string"},
                {"name": "website", "type": "string"},
            ]
    if rules is not None:
        raw["plugins"] = {"inference_rules": rules, "inference_rule_parameters": params or {}}
    return DomainSpec.model_validate(raw)


class FakeGraph:
    """In-memory store executing the trigger's tagged statements and node MERGE syncs."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.obligations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.statements: list[str] = []
        self.constraint_present = True

    async def execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None
    ) -> list[Any]:
        p = parameters or {}
        self.statements.append(cypher.splitlines()[0])
        if "ceg:inference:read_entities" in cypher:
            label = re.search(r"MATCH \(n:(\w+)\)", cypher)[1]  # type: ignore[index]
            return [
                {"entity_id": eid, "props": dict(props)}
                for (lbl, eid), props in self.nodes.items()
                if lbl == label and eid in p["ids"] and props.get("_tenant") == p["tenant"]
            ]
        if "ceg:inference:read_obligations" in cypher:
            return [
                {"entity_id": eid, "obligation": dict(o)}
                for (t, lbl, eid), o in self.obligations.items()
                if t == p["tenant"] and lbl == p["label"] and eid in p["ids"]
            ]
        if "ceg:inference:constraint_check" in cypher:
            return [{"name": trigger.OBLIGATION_CONSTRAINT_NAME}] if self.constraint_present else []
        if "ceg:inference:record_evaluations" in cypher:
            written = []
            for row in p["rows"]:
                o = self.obligations.setdefault((p["tenant"], p["label"], row["entity_id"]), {})
                if (o.get("evidence_revision") or "") != (row["prior_evidence_revision"] or "") or (
                    o.get("rule_revision") or ""
                ) != (row["prior_rule_revision"] or ""):
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
                    attempts=0 if emit else o.get("attempts", 0),
                    wire_key=None if emit else o.get("wire_key"),
                    last_detail=None if emit else o.get("last_detail"),
                )
                written.append({"entity_id": row["entity_id"]})
            return written
        if "ceg:inference:record_delivery" in cypher:
            o = self.obligations.get((p["tenant"], p["label"], p["entity_id"]))
            if o is not None and o.get("pending_key") == p["pending_key"]:
                o.update(status=p["status"], last_detail=p["detail"], attempts=o.get("attempts", 0) + 1)
                if p.get("wire_key") is not None:
                    o["wire_key"] = p["wire_key"]
                if p["status"] == "accepted":
                    o.update(accepted_key=p["pending_key"], accepted_findings_digest=o.get("pending_findings_digest"))
            return []
        merge = re.search(r"MERGE \(n:(\w+) \{(\w+): row(?:\.node)?\.\w+\}\)", cypher)
        if merge and "batch" in p:
            label, id_prop = merge[1], merge[2]
            for row in p["batch"]:
                values = dict(row.get("node", row))
                node = self.nodes.setdefault((label, str(values[id_prop])), {})
                node.update(values)
                node["_tenant"] = p["tenant"]
            return []
        return []


class FakeGate:
    """Gate client double: records packets, answers like EIE's intake handler."""

    def __init__(self, *, queued: int | None = None, error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queued = queued
        self.error = error
        self.release: asyncio.Event | None = None

    async def execute(self, **kwargs: Any) -> Any:
        if self.release is not None:
            await self.release.wait()
        self.sent.append(kwargs)
        if self.error is not None:
            raise self.error
        outputs = kwargs["payload"]["inference_outputs"]
        header = MagicMock(packet_id="pkt", packet_type="response", correlation_id=None)
        response = MagicMock(header=header)
        response.payload = {
            "status": "accepted",
            "targets_queued": len(outputs) if self.queued is None else self.queued,
            "tenant": kwargs["tenant"],
        }
        return response


@pytest.fixture
def flags(monkeypatch: pytest.MonkeyPatch):
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "graph_inference_feedback_enabled", True)
    monkeypatch.setattr(settings, "graph_inference_trigger_enabled", True)
    monkeypatch.setenv("GATE_URL", "http://gate.test")
    return settings


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> FakeGate:
    fake = FakeGate()
    monkeypatch.setattr("engine.gate_egress.get_gate_client", lambda: fake)
    return fake


def _bind(spec: DomainSpec, graph: FakeGraph) -> None:
    from engine.handlers import init_dependencies
    from engine.state import get_state

    loader = MagicMock()
    loader.load_domain.return_value = spec
    get_state().reset()
    init_dependencies(graph, loader)


async def _sync(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from engine.handlers import handle_sync

    result = await handle_sync(TENANT, {"entity_type": "aging-caves", "batch": rows})
    await trigger.drain_inflight()
    return result


@pytest.fixture(autouse=True)
def _reset():
    from engine.state import _reset_singleton

    yield
    _reset_singleton()


# ── Activation boundaries ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_flag_off_by_default_evaluates_nothing(gate: FakeGate, monkeypatch) -> None:
    from engine.config.settings import settings

    assert settings.graph_inference_trigger_enabled is False
    monkeypatch.setattr(settings, "graph_inference_feedback_enabled", True)
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert not any("ceg:inference" in s for s in graph.statements)
    assert gate.sent == []


@pytest.mark.asyncio
async def test_domain_without_declared_rules_evaluates_nothing(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    _bind(_spec(None), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert not any("ceg:inference" in s for s in graph.statements)
    assert gate.sent == []


@pytest.mark.asyncio
async def test_second_synthetic_tenant_carries_no_rule_and_sends_nothing(flags, gate: FakeGate) -> None:
    """P05: generic code holds no domain literal; a tenant that declares nothing gets nothing."""
    spec = synthetic_loader().load_domain("orbital-logistics")
    assert trigger.trigger_active(flags, spec) is False


# ── P22: a changed graph fact produces one cited finding via graph-inference-result ─


@pytest.mark.asyncio
async def test_changed_graph_fact_sends_one_cited_finding(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])

    assert len(gate.sent) == 1
    packet = gate.sent[0]
    assert packet["action"] == GRAPH_INFERENCE_ACTION
    assert packet["tenant"] == TENANT
    [output] = packet["payload"]["inference_outputs"]
    assert (output["entity_id"], output["field"], output["value"]) == ("c1", "company_size_tier", "small")
    assert output["rule"] == "infer_company_size_tier"
    assert output["confidence"] >= 0.55
    assert output["provenance"].startswith(f"ceg:graph:{LABEL}:c1@")
    obligation = graph.obligations[(TENANT, LABEL, "c1")]
    assert packet["idempotency_key"] == obligation["pending_key"]
    assert obligation["status"] == "accepted"


@pytest.mark.asyncio
async def test_graph_read_not_caller_payload_is_the_evidence(flags, gate: FakeGate) -> None:
    """The node as stored is read back: a value the graph already held participates."""
    graph = FakeGraph()
    graph.nodes[(LABEL, "c1")] = {"cave_id": "c1", "employee_count": 120, "_tenant": TENANT}
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "humidity_pct": 92.0}])  # the row itself carries no employee_count
    [output] = gate.sent[0]["payload"]["inference_outputs"]
    assert output["value"] == "mid_market"


# ── P23: unchanged replay, repeated finding, EIE echo, restart ────────────────


@pytest.mark.asyncio
async def test_unchanged_replay_sends_nothing_and_survives_restart(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    spec = _spec(["infer_company_size_tier"])
    _bind(spec, graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert len(gate.sent) == 1

    _bind(spec, graph)  # process restart: state reset, the graph store remains
    graph.statements.clear()
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert len(gate.sent) == 1
    # Read, compared, nothing recorded: an unchanged replay is not new work.
    assert any("read_obligations" in s for s in graph.statements)
    assert not any("record_evaluations" in s for s in graph.statements)
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "accepted"


@pytest.mark.asyncio
async def test_real_change_is_not_suppressed(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    await _sync([{"cave_id": "c1", "employee_count": 120}])
    assert [p["payload"]["inference_outputs"][0]["value"] for p in gate.sent] == ["small", "mid_market"]
    assert gate.sent[0]["idempotency_key"] != gate.sent[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_eie_echo_of_the_finding_sends_nothing(flags, gate: FakeGate) -> None:
    """EIE writes the inferred value back: the graph now holds it, nothing new is proposed."""
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    await _sync([{"cave_id": "c1", "company_size_tier": "small"}])
    assert len(gate.sent) == 1
    assert graph.obligations[(TENANT, LABEL, "c1")]["decision"] == "no_findings"


@pytest.mark.asyncio
async def test_unrelated_change_with_the_same_finding_is_not_resent(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    await _sync([{"cave_id": "c1", "humidity_pct": 88.0}])
    assert len(gate.sent) == 1
    assert graph.obligations[(TENANT, LABEL, "c1")]["decision"] == "same_findings"


@pytest.mark.asyncio
async def test_stale_rule_revision_reevaluates_without_resending_an_accepted_finding(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    _bind(_spec(["infer_company_size_tier"], version="0.3.0"), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    obligation = graph.obligations[(TENANT, LABEL, "c1")]
    assert obligation["decision"] == "same_findings"
    assert len(gate.sent) == 1


# ── P24: only authorized, confident, well-formed findings are sent ────────────


@pytest.mark.asyncio
async def test_exact_allowed_rule_set_runs(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    graph.nodes[(LABEL, "c1")] = {
        "cave_id": "c1",
        "employee_count": 40,
        "website": "https://www.example.org",
        "_tenant": TENANT,
    }
    from engine.handlers import handle_admin

    result = await handle_admin(
        TENANT, {"subaction": "emit_inference_feedback", "entity_type": "aging-caves", "entity_id": "c1"}
    )
    assert result["rules_run"] == ["infer_company_size_tier"]
    assert [o["field"] for o in gate.sent[0]["payload"]["inference_outputs"]] == ["company_size_tier"]


@pytest.mark.asyncio
async def test_empty_rules_means_none(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    graph.nodes[(LABEL, "c1")] = {"cave_id": "c1", "employee_count": 40, "_tenant": TENANT}
    _bind(_spec(["infer_company_size_tier"]), graph)
    from engine.handlers import handle_admin

    result = await handle_admin(
        TENANT, {"subaction": "emit_inference_feedback", "entity_type": "aging-caves", "entity_id": "c1", "rules": []}
    )
    assert result["rules_run"] == []
    assert result["results_produced"] == 0
    assert gate.sent == []


@pytest.mark.asyncio
async def test_unauthorized_rule_is_refused_before_any_read(flags, gate: FakeGate) -> None:
    from engine.handlers import ValidationError, handle_admin

    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    with pytest.raises(ValidationError, match="not authorized by domain"):
        await handle_admin(
            TENANT,
            {
                "subaction": "emit_inference_feedback",
                "entity_type": "aging-caves",
                "entity_id": "c1",
                "rules": ["infer_email_domain_from_website"],
            },
        )
    assert not any("ceg:inference" in s for s in graph.statements)
    assert gate.sent == []


@pytest.mark.asyncio
async def test_caller_entity_is_refused_while_graph_read_is_on(flags, gate: FakeGate) -> None:
    from engine.handlers import ValidationError, handle_admin

    _bind(_spec(["infer_company_size_tier"]), FakeGraph())
    with pytest.raises(ValidationError, match="reads the entity from the graph"):
        await handle_admin(
            TENANT, {"subaction": "emit_inference_feedback", "entity": {"employee_count": 40}, "entity_id": "c1"}
        )
    assert gate.sent == []


@pytest.mark.asyncio
async def test_node_of_another_tenant_is_not_evidence(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    graph.nodes[(LABEL, "c1")] = {"cave_id": "c1", "employee_count": 40, "_tenant": "someone-else"}
    _bind(_spec(["infer_company_size_tier"]), graph)
    from engine.handlers import handle_admin

    result = await handle_admin(
        TENANT, {"subaction": "emit_inference_feedback", "entity_type": "aging-caves", "entity_id": "c1"}
    )
    assert result["trigger"]["decisions"] == {"entity_not_found_for_tenant": 1}
    assert gate.sent == []


@pytest.mark.asyncio
async def test_other_domain_id_is_refused(flags, gate: FakeGate) -> None:
    from engine.handlers import ValidationError, handle_admin

    _bind(_spec(["infer_company_size_tier"]), FakeGraph())
    with pytest.raises(ValidationError, match="does not match the packet tenant"):
        await handle_admin(
            TENANT,
            {"subaction": "emit_inference_feedback", "entity_type": "aging-caves", "entity_id": "c1", "domain_id": "x"},
        )


# ── P28 and failure dispositions: delivery counts only on a complete intake ack ─


@pytest.mark.asyncio
async def test_partial_intake_is_not_delivery_and_is_not_blindly_resent(flags, gate: FakeGate) -> None:
    gate.queued = 1
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier", "infer_email_domain_from_website"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40, "website": "https://www.example.org"}])
    assert len(gate.sent[0]["payload"]["inference_outputs"]) == 2
    obligation = graph.obligations[(TENANT, LABEL, "c1")]
    assert obligation["status"] == "partial"
    assert "accepted_findings_digest" not in obligation
    # A resend could queue the accepted target twice: retry does not run it.
    assert (
        await trigger.retry_pending(
            graph, _spec(["infer_company_size_tier"]), tenant=TENANT, label=LABEL, entity_id="c1"
        )
        is None
    )


@pytest.mark.asyncio
async def test_zero_intake_stays_retryable(flags, gate: FakeGate) -> None:
    gate.queued = 0
    graph = FakeGraph()
    spec = _spec(["infer_company_size_tier"])
    _bind(spec, graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "pending"
    gate.queued = None
    await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1")
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "accepted"
    # Gate cached the zero-intake success under the first key; the retry uses a derived one.
    assert gate.sent[1]["idempotency_key"] == gate.sent[0]["idempotency_key"] + ":zero-intake-retry:1"


@pytest.mark.asyncio
async def test_gate_failure_stays_pending_and_retry_reuses_the_key(flags, gate: FakeGate) -> None:
    gate.error = GateClientError("gate unavailable")
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    obligation = graph.obligations[(TENANT, LABEL, "c1")]
    assert obligation["status"] == "pending"
    first_key = gate.sent[0]["idempotency_key"]

    # An unchanged replay is not new work, even while delivery is outstanding.
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert len(gate.sent) == 1

    gate.error = None
    from engine.handlers import handle_admin

    result = await handle_admin(
        TENANT, {"subaction": "emit_inference_feedback", "entity_type": "aging-caves", "entity_id": "c1", "retry": True}
    )
    assert result["status"] == "inference_feedback_retried"
    assert gate.sent[1]["idempotency_key"] == first_key
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "accepted"


@pytest.mark.asyncio
async def test_gate_not_configured_is_a_blocked_disposition(flags, gate: FakeGate, monkeypatch) -> None:
    monkeypatch.delenv("GATE_URL")
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "blocked"
    assert gate.sent == []


@pytest.mark.asyncio
async def test_sync_answers_before_delivery_runs(flags, gate: FakeGate) -> None:
    """Delivery is never awaited inside the sync that caused it."""
    from engine.handlers import handle_sync

    gate.release = asyncio.Event()
    graph = FakeGraph()
    _bind(_spec(["infer_company_size_tier"]), graph)
    result = await handle_sync(
        TENANT, {"entity_type": "aging-caves", "batch": [{"cave_id": "c1", "employee_count": 40}]}
    )
    assert result["status"] == "success"
    assert gate.sent == []
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "pending"
    gate.release.set()
    await trigger.drain_inflight()
    assert len(gate.sent) == 1


# ── Ack classification and egress hygiene ────────────────────────────────────


# ── Obligation key uniqueness (CTO answer 1) ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_init_schema_adds_the_obligation_key_constraint_only_when_enabled(
    enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.config.settings import settings
    from engine.handlers import _init_schema

    monkeypatch.setattr(settings, "graph_inference_trigger_enabled", enabled)
    graph = FakeGraph()
    await _init_schema(graph, _spec(["infer_company_size_tier"]))  # type: ignore[arg-type]
    expected = trigger.obligation_constraint_cypher()
    assert (expected.splitlines()[0] in graph.statements) is enabled
    assert "REQUIRE (o.tenant, o.entity_type, o.entity_id) IS UNIQUE" in expected


# ── Review findings on #540: concurrency, supersession, constraint ──────────


@pytest.mark.asyncio
async def test_superseded_pending_finding_is_retired_and_never_sent(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    spec = _spec(["infer_company_size_tier"])
    _bind(spec, graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])  # "small" accepted
    gate.error = GateClientError("gate unavailable")
    await _sync([{"cave_id": "c1", "employee_count": 120}])  # "mid_market" pending
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "pending"
    stale = trigger.PendingDelivery(
        entity_id="c1",
        pending_key=graph.obligations[(TENANT, LABEL, "c1")]["pending_key"],
        outputs=json.loads(graph.obligations[(TENANT, LABEL, "c1")]["pending_outputs_json"]),
    )
    gate.error = None
    await _sync([{"cave_id": "c1", "employee_count": 40}])  # back to "small": same as accepted
    obligation = graph.obligations[(TENANT, LABEL, "c1")]
    assert (obligation["decision"], obligation["status"], obligation["pending_key"]) == (
        "same_findings",
        "accepted",
        None,
    )
    sent_before = len(gate.sent)
    assert await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1") is None
    result = await trigger.deliver(graph, spec, tenant=TENANT, label=LABEL, pending=stale)
    assert result["delivery_status"] == "superseded"
    assert len(gate.sent) == sent_before


@pytest.mark.asyncio
async def test_missing_obligation_constraint_writes_nothing(flags, gate: FakeGate) -> None:
    graph = FakeGraph()
    graph.constraint_present = False
    _bind(_spec(["infer_company_size_tier"]), graph)
    await _sync([{"cave_id": "c1", "employee_count": 40}])
    assert graph.obligations == {}
    assert gate.sent == []
    assert not any("record_evaluations" in s for s in graph.statements)


@pytest.mark.asyncio
async def test_init_schema_propagates_a_failed_obligation_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.config.settings import settings
    from engine.handlers import _init_schema

    monkeypatch.setattr(settings, "graph_inference_trigger_enabled", True)

    class _Refusing(FakeGraph):
        async def execute_query(
            self, cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None
        ) -> list[Any]:
            if "ceg_inference_obligation_key" in cypher:
                msg = "Community Edition cannot create this constraint"
                raise RuntimeError(msg)
            return await super().execute_query(cypher, parameters, database)

    with pytest.raises(RuntimeError, match="cannot create this constraint"):
        await _init_schema(_Refusing(), _spec(["infer_company_size_tier"]))  # type: ignore[arg-type]


def test_record_evaluations_statement_carries_every_compare_and_set_predicate() -> None:
    """The fake graph applies the CAS itself; pin that the production statement does too."""
    import inspect

    source = inspect.getsource(trigger.record_evaluations)
    for field in ("evidence_revision", "rule_revision", "accepted_findings_digest"):
        assert f"coalesce(o.{field}, '') = coalesce(row.prior_{field}, '')" in source
    for field in ("wire_key", "last_detail"):
        assert f"o.{field} = CASE WHEN emit THEN null ELSE o.{field} END" in source
    evaluate = inspect.getsource(trigger.evaluate_and_record)
    assert '"prior_accepted_findings_digest": prior.get("accepted_findings_digest")' in evaluate


class CachingGate(FakeGate):
    """Gate that caches every terminal success per idempotency key, like Constellation.Gate."""

    def __init__(self) -> None:
        super().__init__()
        self.cache: dict[str, Any] = {}
        self.queued_targets: list[str] = []
        self.lose_next_response = False

    async def execute(self, **kwargs: Any) -> Any:
        key = kwargs["idempotency_key"]
        if key in self.cache:
            self.sent.append(kwargs)
            return self.cache[key]
        response = await super().execute(**kwargs)
        self.queued_targets += [key] * response.payload["targets_queued"]
        self.cache[key] = response
        if self.lose_next_response:
            self.lose_next_response = False
            msg = "response lost after EIE queued the targets"
            raise GateClientError(msg)
        return response


@pytest.mark.asyncio
async def test_lost_response_after_a_derived_retry_never_queues_twice(flags, monkeypatch: pytest.MonkeyPatch) -> None:
    """CodeRabbit #540: reuse the wire key after an ambiguous outcome; rotate only on a confirmed zero intake."""
    gate = CachingGate()
    monkeypatch.setattr("engine.gate_egress.get_gate_client", lambda: gate)
    graph = FakeGraph()
    spec = _spec(["infer_company_size_tier"])
    _bind(spec, graph)
    gate.queued = 0
    await _sync([{"cave_id": "c1", "employee_count": 40}])  # zero intake under the original key
    gate.queued, gate.lose_next_response = None, True
    await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1")  # queued, response lost
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "pending"
    await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1")  # same derived key
    keys = [packet["idempotency_key"] for packet in gate.sent]
    assert keys[1] == keys[2] != keys[0]
    assert len(gate.queued_targets) == 1
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "accepted"


@pytest.mark.asyncio
async def test_new_obligation_does_not_inherit_the_superseded_delivery_state(flags, gate: FakeGate) -> None:
    """CodeRabbit #540: a new emit clears wire_key and last_detail, so retry sends under its own key."""
    graph = FakeGraph()
    spec = _spec(["infer_company_size_tier"])
    _bind(spec, graph)
    gate.queued = 0
    await _sync([{"cave_id": "c1", "employee_count": 40}])  # zero intake: wire_key + last_detail recorded
    graph.nodes[(LABEL, "c1")]["employee_count"] = 120  # new evidence, recorded but not yet sent
    report = await trigger.evaluate_and_record(
        graph, spec, tenant=TENANT, label=LABEL, id_property="cave_id", ids=["c1"]
    )
    new_key = report.pending[0].pending_key
    obligation = graph.obligations[(TENANT, LABEL, "c1")]
    assert obligation["pending_key"] == new_key != gate.sent[0]["idempotency_key"]
    assert obligation["wire_key"] is None
    assert obligation["last_detail"] is None
    gate.queued = None
    await trigger.retry_pending(graph, spec, tenant=TENANT, label=LABEL, entity_id="c1")
    assert gate.sent[-1]["idempotency_key"] == new_key
    assert graph.obligations[(TENANT, LABEL, "c1")]["status"] == "accepted"
