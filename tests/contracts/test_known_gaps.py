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
The six architectural capability assertions remain strict expected failures until
real implementation and native proof replace them. Schema files can be completed
independently; the implementation gaps are not silently greenwashed.
"""

from __future__ import annotations

import json

import pytest
import yaml

from tests.contracts._constants import CONTRACTS_ROOT

TOOLS = ("match", "sync", "admin", "outcomes", "resolve", "enrich")
ARCH = (
    "eval/eval-pipeline.yaml",
    "eval/benchmarks.yaml",
    "feedback/convergence-loop.yaml",
    "observability/otel-traces.yaml",
    "observability/slo-definitions.yaml",
    "auth/rbac-policy.yaml",
)


@pytest.mark.parametrize("name", TOOLS)
def test_six_source_bound_schema_files_exist(name: str) -> None:
    p = CONTRACTS_ROOT / "agents/tool-schemas" / f"{name}.schema.json"
    s = json.loads(p.read_text())
    assert s["x-source-file"] == f"engine/handlers.py:handle_{name}"
    assert s["x-runtime-validator-applies-this-json-schema"] is False


@pytest.mark.parametrize("path", ARCH)
def test_architecture_doc_preserves_evidence_and_unverified_runtime_state(path: str) -> None:
    p = CONTRACTS_ROOT / path
    d = yaml.safe_load(p.read_text())
    assert d["authority"] == "candidate_extracted_from_source"
    assert d["source_refs"]
    assert d["baseline"]["commit"]
    assert d["operational_proof"]["observed_in_this_pack"] is False
    assert d["operational_proof"]["runtime_test_receipt"] is None
    assert d["contract_status_is_not_runtime_status"] is True


@pytest.mark.parametrize("path", ARCH)
@pytest.mark.xfail(
    strict=True,
    reason="Open architectural capability: source code and runtime proof required; file existence is not closure",
)
def test_operational_gap_remains_open_until_native_proof(path: str) -> None:
    d = yaml.safe_load((CONTRACTS_ROOT / path).read_text())
    # An owner-admitted operational proof must replace this xfail with an actual
    # executable behavior test before it can pass. No placeholder receipts.
    assert d["status"] == "operational_verified"
    assert d["operational_proof"]["observed_in_this_pack"] is True


def test_security_gap_is_explicit_not_relabelled_ready() -> None:
    r = yaml.safe_load((CONTRACTS_ROOT / "auth/rbac-policy.yaml").read_text())
    assert r["release_ready"] is False
    assert r["inconsistency"]["declared_permission"] == "sync:write"
    assert r["inconsistency"]["status"] == "source_remediated_local"
    statuses = {item["id"]: item["status"] for item in r["additional_inconsistencies"]}
    assert statuses["AUTH-CAPSET-02"] == "source_remediated_local"
    assert statuses["AUTH-DELEGATE-04"] == "source_remediated_local"
    assert statuses["AUTH-GATE-03"] == "unverified"


def test_otel_trace_id_does_not_automatically_mean_otel_export() -> None:
    o = yaml.safe_load((CONTRACTS_ROOT / "observability/otel-traces.yaml").read_text())
    assert o["observed"]["otel_span_emission_verified"] is False
    assert o["observed"]["exporter_config_verified"] is False
    assert o["observed"]["sdk_packet_reconstruction_can_create_new_trace_id"] is True
