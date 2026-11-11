"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, contracts]
owner: engine-team
status: active
--- /L9_META ---

Presence of a truthful architectural contract is NOT operational completion.

Each of the six architectural capabilities is in exactly one lifecycle state:

* OPEN (``OPEN_GAPS``): its contract YAML stays ``observed_in_this_pack: false``
  and a strict-xfail probe asserts the missing behaviour. Where the behaviour is
  testable in CEG the probe exercises code, so it XPASSes (and fails CI) when
  the code lands, not when someone edits a YAML. Capabilities whose behaviour
  does not exist anywhere yet keep a contract-flag probe and name the issue
  that tracks the missing work.
* CLOSED (``CLOSED_GAPS``): the YAML is ``operational_verified`` with a CI run
  id and a receipt naming the real behaviour tests, which must exist.

Closing a capability is one edit: move its path from ``OPEN_GAPS`` to
``CLOSED_GAPS`` (with its behaviour tests). The companion evidence test, the
probe and the closure check all key off those two maps.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from tests.contracts._constants import CONTRACTS_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

TOOLS = ("match", "sync", "admin", "outcomes", "resolve", "enrich")

ALL_ARCH = frozenset(
    {
        "eval/eval-pipeline.yaml",
        "eval/benchmarks.yaml",
        "feedback/convergence-loop.yaml",
        "observability/otel-traces.yaml",
        "observability/slo-definitions.yaml",
        "auth/rbac-policy.yaml",
    }
)

# path -> where the missing work is tracked.
OPEN_GAPS: dict[str, str] = {
    "eval/eval-pipeline.yaml": "Quantum-L9/Cognitive.Engine.Graphs#440",
    "eval/benchmarks.yaml": "Quantum-L9/Cognitive.Engine.Graphs#440",
    "feedback/convergence-loop.yaml": "weight-recalculation governance (owner decision pending)",
    "observability/otel-traces.yaml": "Quantum-L9/Cognitive.Engine.Graphs#441, Quantum-L9/Gate_SDK#75",
    "observability/slo-definitions.yaml": "Quantum-L9/Cognitive.Engine.Graphs#441, Quantum-L9/Gate_SDK#75",
    "auth/rbac-policy.yaml": "W2 (CEG-side RBAC), Quantum-L9/Constellation.Gate#37",
}

# path -> behaviour test node ids ("tests/...py::Class::test" or "tests/...py::test")
# that the contract's runtime_test_receipt must list.
CLOSED_GAPS: dict[str, tuple[str, ...]] = {}

ARCH = tuple(sorted(OPEN_GAPS))


# ── Probes for OPEN capabilities ───────────────────────────────────────────
#
# Every probe signals "still open" with AssertionError only; the xfail below is
# restricted to AssertionError so a broken probe (ImportError, TypeError, ...)
# fails CI instead of passing as an expected failure.


def _contract(path: str) -> dict[str, Any]:
    return yaml.safe_load((CONTRACTS_ROOT / path).read_text())


def _contract_flag_probe(path: str) -> Callable[[pytest.MonkeyPatch], Awaitable[None]]:
    """For a capability with no CEG-testable behaviour yet (tracked in OPEN_GAPS)."""

    async def probe(_: pytest.MonkeyPatch) -> None:
        d = _contract(path)
        assert d["status"] == "operational_verified", f"{path}: open, tracked in {OPEN_GAPS.get(path)}"
        assert d["operational_proof"]["observed_in_this_pack"] is True

    return probe


def _wire_engine(capabilities: list[Any], *, feedback_enabled: bool = False) -> MagicMock:
    """Wire engine.handlers to the synthetic orbital-logistics pack and a fake driver."""
    from engine.handlers import init_dependencies
    from engine.state import get_state
    from tests.fixtures.domain_contracts import synthetic_loader

    spec = synthetic_loader().load_domain("orbital-logistics")
    spec = spec.model_copy(
        update={
            "capabilities": capabilities,
            "feedbackloop": spec.feedbackloop.model_copy(update={"enabled": feedback_enabled}),
        }
    )

    async def _echo(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        # One row echoing the parameters, so any write path that reads back
        # its own keys sees them; the probe asserts handler behaviour only.
        parameters = kwargs.get("parameters") or (args[1] if len(args) > 1 else None) or {}
        return [{**parameters, "created": True}]

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=_echo)
    driver.execute_write = AsyncMock(side_effect=_echo)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = ["orbital-logistics"]
    init_dependencies(driver, loader)
    get_state()._tenant_allowlist = {"orbital-logistics"}
    return driver


async def _probe_feedback_convergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remaining blocking item: no learned-weight authority without an approved policy.

    Replay identity, partial-outcome truth and tenant scoping are proven
    (convergence-loop.yaml ``partial_proof``). Still open: once
    ``min_outcomes_for_recalculation`` new outcomes accumulate, the loop writes
    recalculated DimensionWeight weights with no approval, bypassing the governed
    admin ``score_feedback`` -> ``apply_weight_proposal`` path.
    """
    from engine.feedback.convergence import ConvergenceLoop
    from tests.fixtures.domain_contracts import synthetic_loader

    spec = synthetic_loader().load_domain("orbital-logistics")
    spec = spec.model_copy(update={"feedbackloop": spec.feedbackloop.model_copy(update={"enabled": True})})
    loop = ConvergenceLoop(MagicMock(), spec)
    loop._score_propagator.propagate_outcome = AsyncMock(return_value={"propagated": True})  # type: ignore[method-assign]
    loop._weight_calculator.should_recalculate = AsyncMock(return_value=True)  # type: ignore[method-assign]
    loop._weight_calculator.recalculate_weights = AsyncMock(return_value={"fit": 2.0})  # type: ignore[method-assign]
    loop._drift_detector.check_and_alert = AsyncMock(return_value={"drift_detected": False})  # type: ignore[method-assign]
    await loop.on_outcome_recorded({"outcome_id": "probe-1", "candidate_id": "c-probe", "outcome": "success"})
    assert loop._weight_calculator.recalculate_weights.await_count == 0, (
        "the convergence loop applied recalculated weights with no approved proposal"
    )


async def _probe_rbac_revoke_is_tenant_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """rbac-policy.yaml required_proof: revoke restricted to an authorized operator."""
    from engine.auth.capabilities import Capability, get_capability_validator, reset_capability_validator
    from engine.config.schema import CapabilitySpec
    from engine.handlers import ValidationError, handle_admin

    reset_capability_validator()
    try:
        _wire_engine(
            [CapabilitySpec(name="admin_ops", actions=["admin:write"], allowed_subjects=["orbital-logistics"])]
        )
        foreign = Capability(tenant_id="consumer-a", domain_id="*", allowed_actions=frozenset({"sync:write"}))
        validator = get_capability_validator()
        validator.register(foreign)
        try:
            await handle_admin(
                "orbital-logistics", {"subaction": "revoke_capability", "capability_id": foreign.capability_id}
            )
        except ValidationError:
            assert validator._registry[foreign.capability_id].is_active()
            return
        raise AssertionError("tenant revoked another tenant's capability")
    finally:
        reset_capability_validator()


async def _probe_otel_trace_continuity(monkeypatch: pytest.MonkeyPatch) -> None:
    """otel-traces.yaml: no second root on either ingress path, and spans exist."""
    from constellation_node_sdk.runtime.execution import _invoke_handler  # dispatch the SDK uses

    import chassis.handler_registration as registration
    from engine.config.settings import settings
    from engine.packet.chassis_contract import inflate_ingress

    inbound = inflate_ingress(
        action="match", payload={}, tenant="orbital-logistics", trace_id="trace-probe-0001", source_node="gate"
    )
    for audit_enabled in (True, False):
        monkeypatch.setattr(settings, "ingress_packet_audit_enabled", audit_enabled)
        persisted: list[Any] = []

        async def _capture(request: Any, response: Any, _sink: list[Any] = persisted) -> None:
            _sink.append(request)

        async def _engine(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        with patch.object(registration, "_persist", _capture):
            await _invoke_handler(registration._with_packet_audit("match", _engine), inbound)
        assert persisted[0].header.trace_id == "trace-probe-0001", (
            f"ingress_packet_audit_enabled={audit_enabled}: audited request started a new root trace"
        )
    # Span emission and export belong to Gate_SDK (Quantum-L9/Gate_SDK#75).
    assert importlib.util.find_spec("opentelemetry") is not None, "no OpenTelemetry span emission"


PROBES: dict[str, Callable[[pytest.MonkeyPatch], Awaitable[None]]] = {
    "eval/eval-pipeline.yaml": _contract_flag_probe("eval/eval-pipeline.yaml"),
    "eval/benchmarks.yaml": _contract_flag_probe("eval/benchmarks.yaml"),
    "feedback/convergence-loop.yaml": _probe_feedback_convergence,
    "observability/otel-traces.yaml": _probe_otel_trace_continuity,
    "observability/slo-definitions.yaml": _contract_flag_probe("observability/slo-definitions.yaml"),
    "auth/rbac-policy.yaml": _probe_rbac_revoke_is_tenant_bound,
}


# ── Lifecycle bookkeeping ──────────────────────────────────────────────────


def test_every_capability_is_in_exactly_one_lifecycle_state() -> None:
    assert set(OPEN_GAPS).isdisjoint(CLOSED_GAPS)
    assert set(OPEN_GAPS) | set(CLOSED_GAPS) == ALL_ARCH
    assert set(PROBES) == set(OPEN_GAPS), "an OPEN capability needs a probe; a CLOSED one must drop it"


@pytest.mark.parametrize("name", TOOLS)
def test_six_source_bound_schema_files_exist(name: str) -> None:
    p = CONTRACTS_ROOT / "agents/tool-schemas" / f"{name}.schema.json"
    s = json.loads(p.read_text())
    assert s["x-source-file"] == f"engine/handlers.py:handle_{name}"
    assert s["x-runtime-validator-applies-this-json-schema"] is False


@pytest.mark.parametrize("path", ARCH)
def test_architecture_doc_preserves_evidence_and_unverified_runtime_state(path: str) -> None:
    d = _contract(path)
    assert d["authority"] == "candidate_extracted_from_source"
    assert d["source_refs"]
    assert d["baseline"]["commit"]
    assert d["operational_proof"]["observed_in_this_pack"] is False
    assert d["operational_proof"]["runtime_test_receipt"] is None
    assert d["contract_status_is_not_runtime_status"] is True


@pytest.mark.parametrize("path", ARCH)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Open architectural capability: the probe asserts behaviour that does not exist yet",
)
async def test_operational_gap_remains_open_until_native_proof(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # XPASS means the behaviour landed: move the path to CLOSED_GAPS with its
    # real behaviour tests and an owner-admitted CI receipt. No placeholder receipts.
    await PROBES[path](monkeypatch)


_NODE_ID = re.compile(r"^(tests/[\w/]+\.py)::(?:(\w+)::)?(\w+)$")


@pytest.mark.parametrize("path", sorted(CLOSED_GAPS))
def test_closed_capability_carries_ci_receipt_and_real_tests(path: str) -> None:
    d = _contract(path)
    proof = d["operational_proof"]
    assert d["status"] == "operational_verified"
    assert proof["observed_in_this_pack"] is True
    assert proof["CI_run_id"], "closure requires the CI run that executed the behaviour tests"
    receipt = proof["runtime_test_receipt"]
    assert receipt, "closure requires a runtime test receipt"
    assert set(CLOSED_GAPS[path]) <= set(receipt["tests"])
    for node_id in CLOSED_GAPS[path]:
        m = _NODE_ID.match(node_id)
        assert m, node_id
        source = (REPO_ROOT / m.group(1)).read_text()
        if m.group(2):
            assert re.search(rf"^class {m.group(2)}\b", source, re.M), node_id
        assert re.search(rf"^\s*(async )?def {m.group(3)}\b", source, re.M), node_id


def test_security_gap_is_explicit_not_relabelled_ready() -> None:
    r = _contract("auth/rbac-policy.yaml")
    assert r["release_ready"] is False
    assert r["inconsistency"]["declared_permission"] == "sync:write"
    assert r["inconsistency"]["status"] == "source_remediated_local"
    statuses = {item["id"]: item["status"] for item in r["additional_inconsistencies"]}
    assert statuses["AUTH-CAPSET-02"] == "source_remediated_local"
    assert statuses["AUTH-DELEGATE-04"] == "source_remediated_local"
    assert statuses["AUTH-GATE-03"] == "unverified"


def test_otel_trace_id_does_not_automatically_mean_otel_export() -> None:
    o = _contract("observability/otel-traces.yaml")
    assert o["observed"]["otel_span_emission_verified"] is False
    assert o["observed"]["exporter_config_verified"] is False
    assert o["observed"]["sdk_packet_reconstruction_can_create_new_trace_id"] is True
