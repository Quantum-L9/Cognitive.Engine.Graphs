"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-admission, validator-parity, catalogue]
owner: engine-team
status: active
--- /L9_META ---

Admission-rule catalogue and validator parity (issue #400, WS-B items B3 and B4).

`docs/contracts/data/models/domain-admission-rules.json` lists the admission
rules a JSON Schema cannot express. Every negative case below violates one
catalogue rule in a spec built from the synthetic packs under
`tests/fixtures/domains` and compares three verdicts on the same bytes:

* the validator: `tools/validate_domain.py --complete --require-reviewed`;
* runtime admission of a published revision: `DomainPackLoader` on the receipt
  layout (`domain_pack_receipts_enabled`), which must refuse with the validator's
  message;
* runtime admission with default settings on the legacy layout, which must reach
  the verdict the catalogue records for that rule (`runtime.default_loader`).

The drift tests fail when a catalogue rule has no negative case, a case names an
unknown rule, or a catalogue code or test reference no longer resolves.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.config import loader as owner
from engine.config.schema import DomainSpec
from tools import validate_domain
from tools.validate_domain import admission_rules, validate_spec

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "domains"
TENANT = "orbital-logistics"
REVIEWED = "0.2.0-reviewed"
RULES: dict[str, dict[str, Any]] = {rule["id"]: rule for rule in admission_rules()["rules"]}

Raw = dict[str, Any]

# A relationship endpoint over the fixture's (OrbitalDepot)-[:RESUPPLIED_BY]->(OrbitalDepot) edge.
_RELATIONSHIP: Raw = {
    "path": "/v1/sync/resupply",
    "method": "POST",
    "targetedge": "RESUPPLIED_BY",
    "fromnode": "OrbitalDepot",
    "tonode": "OrbitalDepot",
    "batchstrategy": "UNWINDMERGE",
}
_OBSERVED: list[Raw] = [
    {
        "provider": "crm",
        "fromobjecttype": "depots",
        "toobjecttype": "depots",
        "associationcategory": "USER_DEFINED",
        "associationtypeid": 1,
    }
]


def _reviewed(domain_id: str = TENANT) -> Raw:
    raw: Raw = yaml.safe_load((FIXTURES / domain_id / "spec.yaml").read_bytes())
    raw["domain"]["version"] = REVIEWED
    return raw


def _node(raw: Raw, label: str) -> Raw:
    return next(node for node in raw["ontology"]["nodes"] if node["label"] == label)


def _edge(raw: Raw, edge_type: str) -> Raw:
    return next(edge for edge in raw["ontology"]["edges"] if edge["type"] == edge_type)


def _endpoint(raw: Raw, target: str) -> Raw:
    return next(endpoint for endpoint in raw["sync"]["endpoints"] if endpoint.get("targetnode") == target)


def _gate(raw: Raw, name: str) -> Raw:
    return next(gate for gate in raw["gates"] if gate["name"] == name)


def _dimension(raw: Raw, name: str) -> Raw:
    return next(dim for dim in raw["scoring"]["dimensions"] if dim["name"] == name)


def _add_relationship(raw: Raw, **extra: Any) -> None:
    raw["sync"]["endpoints"].append({**_RELATIONSHIP, **extra})


@dataclass(frozen=True)
class Case:
    """One spec that violates exactly one catalogue rule."""

    rule: str
    name: str
    mutate: Callable[[Raw], None] | None = None
    # Applied to the serialized bytes, for defects YAML cannot round-trip.
    rebytes: Callable[[bytes], bytes] | None = None
    tenant: str = TENANT
    settings: dict[str, Any] = field(default_factory=dict)
    symlink: bool = False
    receipt_patch: dict[str, Any] = field(default_factory=dict)
    # False when the refusal message carries a filesystem path.
    exact: bool = True

    @property
    def id(self) -> str:
        return f"{self.rule}:{self.name}"

    def data(self) -> bytes:
        raw = _reviewed()
        if self.mutate is not None:
            self.mutate(raw)
        data = yaml.safe_dump(raw, sort_keys=False).encode()
        return self.rebytes(data) if self.rebytes is not None else data


def _set(path: str, value: Any) -> Callable[[Raw], None]:
    """Set `a.b.c` in the raw spec; the last key is created or replaced."""

    def mutate(raw: Raw) -> None:
        *parents, leaf = path.split(".")
        target: Any = raw
        for key in parents:
            target = target[key]
        target[leaf] = value

    return mutate


def _both_ids(raw: Raw) -> None:
    raw["domain"]["id"] = "Orbital_Logistics"


def _other_id(raw: Raw) -> None:
    raw["domain"]["id"] = "lunar-logistics"


def _hop_cap(raw: Raw) -> None:
    raw["traversal"] = {"steps": [{"name": "far", "pattern": "(candidate)-[:STOCKS*1..999]->(g:PropellantGrade)"}]}


def _hop_open(raw: Raw) -> None:
    raw["traversal"] = {"steps": [{"name": "open", "pattern": "(candidate)-[:STOCKS*1..]->(g:PropellantGrade)"}]}


def _list_without_items(raw: Raw) -> None:
    _node(raw, "PropellantGrade")["properties"].append({"name": "lots", "type": "list"})


def _list_of_lists(raw: Raw) -> None:
    _node(raw, "PropellantGrade")["properties"].append(
        {"name": "lots", "type": "list", "items": {"type": "list", "items": {"type": "string"}}}
    )


def _list_query_field(raw: Raw) -> None:
    raw["queryschema"]["fields"].append({"name": "orbits", "type": "list"})


def _list_derived_parameter(raw: Raw) -> None:
    raw["derivedparameters"] = [{"name": "orbits", "type": "list", "expression": "1"}]


def _calibration_range(raw: Raw) -> None:
    raw["calibration"] = {
        "pairs": [{"node_a": "a", "node_b": "b", "expected_score_min": 0.9, "expected_score_max": 0.1}]
    }


def _duplicate_node(raw: Raw) -> None:
    raw["ontology"]["nodes"].append(copy.deepcopy(_node(raw, "PropellantGrade")))


def _duplicate_edge(raw: Raw) -> None:
    raw["ontology"]["edges"].append(copy.deepcopy(_edge(raw, "STOCKS")))


def _duplicate_gate(raw: Raw) -> None:
    raw["gates"].append(copy.deepcopy(raw["gates"][0]))


def _exclusion_unknown_edge(raw: Raw) -> None:
    raw["gates"].append(
        {"name": "not_grounded", "type": "exclusion", "edgetype": "GROUNDS", "candidateprop": "depot_id"}
    )


def _refnamespace_on_relationship(raw: Raw) -> None:
    _add_relationship(raw, refnamespace="depot")


def _refnamespace_inconsistent(raw: Raw) -> None:
    grades = _endpoint(raw, "OrbitClass")
    grades["refnamespace"] = "orbit"
    raw["sync"]["endpoints"].append({**grades, "path": "/v1/sync/orbit-classes-2", "refnamespace": "other"})


def _refnamespace_two_labels(raw: Raw) -> None:
    raw["ontology"]["nodes"].append(
        {"label": "Tug", "managedby": "sync", "properties": [{"name": "tug_id", "type": "string", "required": True}]}
    )
    raw["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/tugs",
            "method": "POST",
            "targetnode": "Tug",
            "idproperty": "tug_id",
            "batchstrategy": "UNWINDMERGE",
            "refnamespace": "shared",
        }
    )
    _endpoint(raw, "OrbitalDepot")["refnamespace"] = "shared"


def _namespaced_child(raw: Raw) -> None:
    _endpoint(raw, "PropellantGrade")["refnamespace"] = "grade"


def _observed_empty(raw: Raw) -> None:
    _add_relationship(raw, observedfrom=[])


def _observed_without_namespace(raw: Raw) -> None:
    _add_relationship(raw, observedfrom=_OBSERVED)


def _association_twice(raw: Raw) -> None:
    _endpoint(raw, "OrbitalDepot")["refnamespace"] = "depot"
    _add_relationship(raw, observedfrom=_OBSERVED)
    _add_relationship(raw, path="/v1/sync/resupply-2", observedfrom=_OBSERVED)


def _relevance_without_writer(raw: Raw) -> None:
    _edge(raw, "RESUPPLIED_BY")["evidencerelevance"] = 0.5


def _relevance_inconsistent(raw: Raw) -> None:
    _edge(raw, "RESUPPLIED_BY")["evidencerelevance"] = 0.5
    _add_relationship(raw)
    raw["ontology"]["edges"].append(
        {
            "type": "RESUPPLIED_BY",
            "from": "OrbitalDepot",
            "to": "PropellantGrade",
            "direction": "DIRECTED",
            "category": "transaction",
            "managedby": "sync",
        }
    )


def _relevance_edge_not_sync_managed(raw: Raw) -> None:
    edge = _edge(raw, "RESUPPLIED_BY")
    edge["evidencerelevance"] = 0.5
    edge["managedby"] = "gds"
    _add_relationship(raw)


def _geodecay_without_properties(raw: Raw) -> None:
    dim = _dimension(raw, "depot_reliability")
    dim["computation"] = "geodecay"
    del dim["candidateprop"]


def _composite_without_subgates(raw: Raw) -> None:
    raw["gates"].append({"name": "combined", "type": "composite"})


def _traversal_gate_without_edgetype(raw: Raw) -> None:
    raw["gates"].append({"name": "linked", "type": "traversal"})


def _unregistered_rule(raw: Raw) -> None:
    raw["plugins"] = {"inference_rules": ["infer_nothing_known"]}


def _unauthorized_rule_parameters(raw: Raw) -> None:
    raw["plugins"] = {"inference_rule_parameters": {"infer_buyer_persona": {}}}


def _unconsumed_map(raw: Raw) -> None:
    _node(raw, "PropellantGrade")["properties"].append(
        {"name": "assay", "type": "map", "properties": [{"name": "lab", "type": "string"}]}
    )


def _structural_similarity(raw: Raw) -> None:
    _dimension(raw, "depot_reliability")["cold_start_fallback"] = "structural_similarity"


def _attention_without_success_value(raw: Raw) -> None:
    raw["scoring"]["dimensions"].append(
        {
            "name": "attention",
            "source": "computed",
            "computation": "preference_attention",
            "candidateprop": "reliability",
            "weightkey": "w_attention",
            "defaultweight": 0.05,
        }
    )


def _traversal_keyword(raw: Raw) -> None:
    raw["traversal"] = {"steps": [{"name": "purge", "pattern": "(candidate)-[:STOCKS]->(g:PropellantGrade) DELETE g"}]}


def _unbound_gate_parameter(raw: Raw) -> None:
    raw["queryschema"]["fields"] = []
    _gate(raw, "enough_ports")["queryparam"] = "min_berths"


def _traversal_gate_undeclared_edge(raw: Raw) -> None:
    raw["gates"].append({"name": "linked", "type": "traversal", "edgetype": "TETHERED_TO"})


def _traversal_undeclared_label(raw: Raw) -> None:
    raw["traversal"] = {"steps": [{"name": "tether", "pattern": "(candidate)-[:TETHERED_TO]->(t:Tether)"}]}


def _blocked_gate_field(raw: Raw) -> None:
    raw["compliance"] = {"prohibitedfactors": {"enabled": True, "blockedfields": ["accepts_hazmat"]}}


def _blocked_scoring_field(raw: Raw) -> None:
    raw["compliance"] = {"prohibitedfactors": {"enabled": True, "blockedfields": ["reliability"]}}


def _relationship_edge_not_sync_managed(raw: Raw) -> None:
    _edge(raw, "RESUPPLIED_BY")["managedby"] = "gds"
    _add_relationship(raw)


def _duplicate_top_level_key(data: bytes) -> bytes:
    return data + b"gates: []\n"


def _oversize(data: bytes) -> bytes:
    return data + b"#" + b"x" * owner.MAX_SPEC_BYTES + b"\n"


CASES: list[Case] = [
    # read
    Case("ADM-READ-REGULAR-FILE", "symlinked-spec", symlink=True, exact=False),
    Case("ADM-READ-SIZE-LIMIT", "over-5-mib", rebytes=_oversize),
    Case("ADM-YAML-WELL-FORMED", "duplicate-key", rebytes=_duplicate_top_level_key),
    Case("ADM-YAML-MAPPING-ROOT", "sequence-root", rebytes=lambda _data: b"- not\n- a mapping\n"),
    Case("ADM-COMPLETE-VALIDATION-GUARDS", "guard-disabled", settings={"gate_role_exemption": False}),
    Case("ADM-COMPLETE-TOP-LEVEL-KEYS", "unknown-section", _set("launchpads", [])),
    # identity
    Case("ADM-ID-TENANT-GRAMMAR", "illegal-id", _both_ids, tenant="Orbital_Logistics"),
    Case("ADM-ID-MATCHES-TENANT", "id-differs-from-directory", _other_id),
    Case("ADM-STAGE-REVIEWED", "unsuffixed-version", _set("domain.version", "0.2.0")),
    Case("ADM-STAGE-REVIEWED", "proposed-version", _set("domain.version", "0.2.0-proposed")),
    # model
    Case("ADM-MODEL-VERSION-SEMVER", "free-form-version", _set("domain.version", "latest")),
    Case("ADM-MODEL-TRAVERSAL-HOP-BOUNDS", "above-hard-cap", _hop_cap),
    Case("ADM-MODEL-TRAVERSAL-HOP-BOUNDS", "open-range", _hop_open),
    Case("ADM-MODEL-GATE-KEYS-KNOWN", "null_behavior", lambda raw: raw["gates"][0].update(null_behavior="fail")),
    Case("ADM-MODEL-GATE-OPERATOR-ALLOWED", "like", lambda raw: _gate(raw, "enough_ports").update(operator="LIKE")),
    Case(
        "ADM-MODEL-GATE-QUERYPARAM-SHAPE",
        "list-queryparam",
        lambda raw: _gate(raw, "enough_ports").update(queryparam=["min_ports"]),
    ),
    Case("ADM-MODEL-PROPERTY-SHAPE", "list-without-items", _list_without_items),
    Case("ADM-MODEL-PROPERTY-SHAPE", "list-of-lists", _list_of_lists),
    Case("ADM-MODEL-QUERY-PARAMETER-SCALAR", "list-query-field", _list_query_field),
    Case("ADM-MODEL-QUERY-PARAMETER-SCALAR", "list-derived-parameter", _list_derived_parameter),
    Case("ADM-MODEL-CALIBRATION-RANGE", "min-above-max", _calibration_range),
    Case(
        "ADM-MODEL-EVIDENCERELEVANCE-NUMBER",
        "bool-relevance",
        lambda raw: _edge(raw, "RESUPPLIED_BY").update(evidencerelevance=True),
    ),
    # cross-reference
    Case("ADM-XREF-ONTOLOGY-UNIQUE", "duplicate-node-label", _duplicate_node),
    Case("ADM-XREF-ONTOLOGY-UNIQUE", "duplicate-edge-signature", _duplicate_edge),
    Case(
        "ADM-XREF-MATCH-ENTITY-LABELS",
        "candidate",
        lambda raw: raw["matchentities"]["candidate"][0].update(label="Tug"),
    ),
    Case(
        "ADM-XREF-MATCH-ENTITY-LABELS",
        "queryentity",
        lambda raw: raw["matchentities"]["queryentity"][0].update(label="Tug"),
    ),
    Case("ADM-XREF-EDGE-ENDPOINTS", "unknown-source", lambda raw: _edge(raw, "STOCKS").update({"from": "Tug"})),
    Case("ADM-XREF-GATE-NAMES-UNIQUE", "repeated-gate", _duplicate_gate),
    Case("ADM-XREF-EXCLUSION-EDGE-TYPE", "unknown-edge", _exclusion_unknown_edge),
    Case(
        "ADM-XREF-GATE-PARAMETER-DECLARED",
        "undeclared-parameter",
        lambda raw: _gate(raw, "enough_ports").update(queryparam="min_berths"),
    ),
    Case(
        "ADM-XREF-WEIGHT-CEILING",
        "sum-above-one",
        lambda raw: _dimension(raw, "depot_reliability").update(defaultweight=0.9),
    ),
    Case(
        "ADM-XREF-GDS-PROJECTION",
        "unknown-node-label",
        lambda raw: raw["gdsjobs"][0]["projection"].update(nodelabels=["Tug"]),
    ),
    # tenant mapping
    Case("ADM-MAP-REFNAMESPACE-NODE-ENDPOINT-ONLY", "on-relationship-endpoint", _refnamespace_on_relationship),
    Case("ADM-MAP-REFNAMESPACE-CONSISTENT", "two-namespaces-one-label", _refnamespace_inconsistent),
    Case("ADM-MAP-REFNAMESPACE-UNIQUE", "one-namespace-two-labels", _refnamespace_two_labels),
    Case("ADM-MAP-NAMESPACED-LABEL-DIRECT-WRITE", "childsync-target", _namespaced_child),
    Case("ADM-MAP-OBSERVEDFROM-NON-EMPTY", "empty-list", _observed_empty),
    Case("ADM-MAP-OBSERVED-NODE-NAMESPACED", "no-refnamespace", _observed_without_namespace),
    Case("ADM-MAP-ASSOCIATION-UNIQUE", "mapped-twice", _association_twice),
    Case("ADM-MAP-RELEVANCE-WRITER", "no-targetedge-endpoint", _relevance_without_writer),
    Case("ADM-MAP-RELEVANCE-CONSISTENT", "signatures-disagree", _relevance_inconsistent),
    Case("ADM-MAP-RELATIONSHIP-DECLARATION", "edge-not-sync-managed", _relevance_edge_not_sync_managed),
    # withholding
    Case("ADM-WITHHOLD-DORMANT", "dormant", _set("domain.activation", "dormant")),
    Case("ADM-DEFECT-STRUCTURAL-PROPERTY", "geodecay-without-properties", _geodecay_without_properties),
    Case("ADM-DEFECT-GATE-STRICTWHEN", "strictwhen", lambda raw: raw["gates"][0].update(strictwhen="hazmat")),
    Case("ADM-DEFECT-GATE-ROLE-FIELDS", "roleexempt", lambda raw: raw["gates"][0].update(roleexempt=["admin"])),
    Case("ADM-DEFECT-GATE-ROLE-FIELDS", "relaxedpenalty", lambda raw: raw["gates"][0].update(relaxedpenalty=0.1)),
    Case("ADM-DEFECT-GATE-COMPILES", "composite-without-subgates", _composite_without_subgates),
    Case("ADM-DEFECT-GATE-COMPILES", "traversal-gate-without-edgetype", _traversal_gate_without_edgetype),
    Case("ADM-DEFECT-PLUGIN-RULES", "unregistered-rule", _unregistered_rule),
    Case("ADM-DEFECT-PLUGIN-RULES", "unauthorized-parameters", _unauthorized_rule_parameters),
    Case("ADM-DEFECT-SYNC-SHAPE", "unconsumed-map", _unconsumed_map),
    Case(
        "ADM-DEFECT-SCORING-PRODUCER",
        "inherit-prior",
        lambda raw: _dimension(raw, "depot_reliability").update(null_strategy="inherit_prior"),
    ),
    Case("ADM-DEFECT-SCORING-PRODUCER", "structural-similarity", _structural_similarity),
    Case("ADM-DEFECT-SCORING-COMPILES", "compiler-refusal", _attention_without_success_value),
    Case(
        "ADM-DEFECT-SCORING-COMPILES",
        "unbound-parameter",
        lambda raw: _dimension(raw, "depot_reliability").update(null_strategy="population_mean"),
    ),
    Case("ADM-DEFECT-MATCH-DIRECTION", "traversal-refused", _traversal_keyword),
    Case("ADM-DEFECT-MATCH-DIRECTION", "unbound-gate-parameter", _unbound_gate_parameter),
    Case("ADM-COMPLETE-GATE-EDGE-DECLARED", "traversal-gate-undeclared-edge", _traversal_gate_undeclared_edge),
    Case("ADM-COMPLETE-TRAVERSAL-WARNINGS", "undeclared-label", _traversal_undeclared_label),
    Case("ADM-COMPLETE-PROHIBITED-FACTORS", "blocked-gate-field", _blocked_gate_field),
    Case("ADM-COMPLETE-PROHIBITED-FACTORS", "blocked-scoring-field", _blocked_scoring_field),
    Case("ADM-COMPLETE-SYNC-OWNER", "relationship-edge-not-sync-managed", _relationship_edge_not_sync_managed),
    # receipt
    Case("ADM-RECEIPT-BINDS-BYTES", "other-reviewed-bytes", receipt_patch={"reviewed_input_sha256": "b" * 64}),
    Case("ADM-RECEIPT-BINDS-BYTES", "other-version", receipt_patch={"version": "9.9.9-reviewed"}),
]


@dataclass(frozen=True)
class Verdicts:
    """What the validator and the two runtime loaders decided for one spec."""

    cli_code: int
    cli_results: list[dict[str, Any]]
    receipt_runtime: Exception | DomainSpec
    default_runtime: Exception | DomainSpec

    @property
    def cli_message(self) -> str:
        return str(self.cli_results[0]["message"])


def _place(directory: Path, data: bytes, *, symlink: bool) -> Path:
    directory.mkdir(parents=True)
    path = directory / "spec.yaml"
    if symlink:
        real = directory.parent / f"{directory.name}-real.yaml"
        real.write_bytes(data)
        path.symlink_to(real)
    else:
        path.write_bytes(data)
    return path


def _declared_version(data: bytes) -> str:
    try:
        return str(yaml.safe_load(data)["domain"]["version"])
    except Exception:
        return REVIEWED


def _receipt(tenant: str, data: bytes, patch: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    return {
        "schema": "ceg.domain-approval/v1",
        "tenant_id": tenant,
        "version": _declared_version(data),
        "sha256": digest,
        "reviewed_input_sha256": digest,
        "reviewed_by": "TEST ONLY Reviewer",
        "approval_ref": "test-fixture:no-real-approval",
        "reviewed_at": "2026-01-01T00:00:00+00:00",
        "previous_sha256": None,
        **patch,
    }


def _publish(root: Path, tenant: str, data: bytes, *, symlink: bool, patch: dict[str, Any]) -> None:
    """A disposable receipt-layout store holding exactly these bytes (unsigned test evidence)."""
    digest = hashlib.sha256(data).hexdigest()
    revision = root / tenant / "revisions" / digest
    _place(revision, data, symlink=symlink)
    (revision / "receipt.json").write_text(json.dumps(_receipt(tenant, data, patch)))
    pointer = {
        "schema": "ceg.domain-current/v1",
        "tenant_id": tenant,
        "version": _declared_version(data),
        "sha256": digest,
    }
    (root / tenant / "current.json").write_text(json.dumps(pointer))


def _load(root: Path, tenant: str) -> Exception | DomainSpec:
    try:
        return owner.DomainPackLoader(str(root)).load_domain(tenant)
    except (owner.DomainSpecError, owner.DomainNotFoundError) as exc:
        return exc


def _verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    *,
    tenant: str = TENANT,
    symlink: bool = False,
    settings: dict[str, Any] | None = None,
    receipt_patch: dict[str, Any] | None = None,
) -> Verdicts:
    patch = receipt_patch or {}
    for name, value in (settings or {}).items():
        monkeypatch.setattr(owner.settings, name, value)
    spec_path = _place(tmp_path / "packs" / tenant, data, symlink=symlink)
    receipt_path: Path | None = None
    if patch:
        receipt_path = tmp_path / "receipt.json"
        receipt_path.write_text(json.dumps(_receipt(tenant, data, patch)))
    cli_results, cli_code = validate_spec(spec_path, complete=True, require_reviewed=True, receipt_path=receipt_path)

    default_runtime = _load(tmp_path / "packs", tenant)

    _publish(tmp_path / "store", tenant, data, symlink=symlink, patch=patch)
    monkeypatch.setattr(owner.settings, "domain_pack_receipts_enabled", True)
    receipt_runtime = _load(tmp_path / "store", tenant)
    return Verdicts(cli_code, cli_results, receipt_runtime, default_runtime)


@pytest.fixture(autouse=True)
def _default_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the documented defaults so an ambient environment cannot decide a verdict."""
    for name in (
        "unvalidated_domain_packs_enabled",
        "dormant_domain_packs_enabled",
        "scoring_admission_check_enabled",
        "admission_checks_full_match",
        "domain_pack_receipts_enabled",
        "domain_production_stage_required",
    ):
        monkeypatch.setattr(owner.settings, name, False)
    monkeypatch.setattr(owner.settings, "l9_env", "dev")


# ── Positive parity ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("domain_id", ["orbital-logistics", "cheese-aging"])
def test_reviewed_synthetic_pack_is_admitted_by_validator_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain_id: str
) -> None:
    data = yaml.safe_dump(_reviewed(domain_id), sort_keys=False).encode()
    verdicts = _verdicts(tmp_path, monkeypatch, data, tenant=domain_id)

    assert verdicts.cli_code == 0
    assert [(r["check"], r["status"]) for r in verdicts.cli_results] == [("native-admission", "PASS")]
    for runtime in (verdicts.receipt_runtime, verdicts.default_runtime):
        assert isinstance(runtime, DomainSpec)
        assert (runtime.domain.id, runtime.domain.version) == (domain_id, REVIEWED)


@pytest.mark.parametrize("domain_id", ["orbital-logistics", "cheese-aging"])
def test_committed_fixture_bytes_reach_one_verdict_under_complete(tmp_path: Path, domain_id: str) -> None:
    """The unreviewed fixture as committed: `--complete` and the default loader both admit it."""
    data = (FIXTURES / domain_id / "spec.yaml").read_bytes()
    spec_path = _place(tmp_path / domain_id, data, symlink=False)

    _results, code = validate_spec(spec_path, complete=True)

    assert code == 0
    assert isinstance(_load(tmp_path, domain_id), DomainSpec)


# ── Negative parity, one catalogue rule per case ─────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_validator_refuses_what_runtime_admission_refuses(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = RULES[case.rule]
    pattern = re.compile(rule["validator"]["message_pattern"])
    verdicts = _verdicts(
        tmp_path,
        monkeypatch,
        case.data(),
        tenant=case.tenant,
        symlink=case.symlink,
        settings=case.settings,
        receipt_patch=case.receipt_patch,
    )

    # The validator refuses with the catalogue's check id, status, exit code and message class.
    assert verdicts.cli_code == rule["validator"]["exit_code"] == 1
    assert len(verdicts.cli_results) == 1
    assert verdicts.cli_results[0]["check"] == rule["validator"]["check"]
    assert verdicts.cli_results[0]["status"] == rule["validator"]["status"]
    assert pattern.search(verdicts.cli_message), verdicts.cli_message

    # Runtime admission of the published revision refuses for the same reason.
    assert rule["runtime"]["receipt_loader"] == "refused"
    refusal = verdicts.receipt_runtime
    assert isinstance(refusal, Exception), "receipt-layout runtime admitted a spec the validator refused"
    assert type(refusal).__name__ == rule["validator"]["error_class"]
    assert pattern.search(str(refusal)), str(refusal)
    if case.exact:
        assert str(refusal) == verdicts.cli_message

    # Default-settings runtime reaches the verdict the catalogue records.
    default = "refused" if isinstance(verdicts.default_runtime, Exception) else "admitted"
    assert default == rule["runtime"]["default_loader"], verdicts.default_runtime


_WITHHOLDING = [case for case in CASES if RULES[case.rule]["stage"] == "withholding"]


@pytest.mark.parametrize("case", _WITHHOLDING, ids=[case.id for case in _WITHHOLDING])
def test_validator_message_is_the_loader_withholding_reason(case: Case, tmp_path: Path) -> None:
    """A withheld pack parses; the validator reports `withholding_reason` verbatim."""
    spec = DomainSpec.model_validate(yaml.safe_load(case.data()))
    spec_path = _place(tmp_path / TENANT, case.data(), symlink=False)

    results, code = validate_spec(spec_path, complete=True, require_reviewed=True)

    assert code == 1
    assert results[0]["message"] == owner.withholding_reason(spec, complete=True)
    if case.rule != "ADM-WITHHOLD-DORMANT":
        defects = owner.admission_defects(spec, complete=True)
        assert defects
        assert all(defect in results[0]["message"] for defect in defects)


_OVERRIDABLE = [case for case in CASES if RULES[case.rule]["runtime"]["operator_override"]]


@pytest.mark.parametrize("case", _OVERRIDABLE, ids=[case.id for case in _OVERRIDABLE])
def test_operator_override_serves_at_runtime_and_never_relaxes_complete(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = RULES[case.rule]["runtime"]["operator_override"]
    setting = override.lower()
    assert setting in type(owner.settings).model_fields
    monkeypatch.setattr(owner.settings, setting, True)
    spec_path = _place(tmp_path / TENANT, case.data(), symlink=False)

    _results, code = validate_spec(spec_path, complete=True, require_reviewed=True)

    assert code == 1
    assert isinstance(_load(tmp_path, TENANT), DomainSpec)


def test_require_reviewed_alone_matches_the_runtime_stage_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--require-reviewed` without `--complete` is the runtime production-stage flag."""
    data = (FIXTURES / TENANT / "spec.yaml").read_bytes()
    spec_path = _place(tmp_path / TENANT, data, symlink=False)

    results, code = validate_spec(spec_path, require_reviewed=True)

    monkeypatch.setattr(owner.settings, "l9_env", "prod")
    monkeypatch.setattr(owner.settings, "domain_production_stage_required", True)
    runtime = _load(tmp_path, TENANT)
    assert code == 1
    assert isinstance(runtime, owner.DomainSpecError)
    assert results[0]["message"] == str(runtime)
    assert re.search(RULES["ADM-STAGE-REVIEWED"]["validator"]["message_pattern"], str(runtime))


# ── Catalogue drift ──────────────────────────────────────────────────────────


# ── What the advertised commands do and do not evaluate (review of #474) ───────


def _main(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    with pytest.raises(SystemExit) as exit_info:
        validate_domain.main(argv)
    return int(exit_info.value.code or 0), json.loads(capsys.readouterr().out)


def test_command_without_receipt_does_not_evaluate_the_receipt_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`validator.command` passes a spec whose receipt is wrong; `receipt_command` refuses it."""
    data = yaml.safe_dump(_reviewed(), sort_keys=False).encode()
    spec_path = _place(tmp_path / "packs" / TENANT, data, symlink=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt(TENANT, data, {"reviewed_input_sha256": "b" * 64})))
    common = [str(spec_path), "--root", str(tmp_path), "--complete", "--require-reviewed", "--json"]

    code, out = _main(common, capsys)
    assert (code, out["results"][0]["status"]) == (0, "PASS")

    code, out = _main([*common, "--receipt", str(receipt_path)], capsys)
    assert code == 1
    assert re.search(RULES["ADM-RECEIPT-BINDS-BYTES"]["validator"]["message_pattern"], out["results"][0]["message"])


def test_catalogue_names_the_receipt_command_and_scopes_the_verdict() -> None:
    validator = admission_rules()["validator"]
    assert "--receipt" not in validator["command"]
    assert "--receipt" in validator["receipt_command"]
    assert "--complete" in validator["receipt_command"]
    assert "--require-reviewed" in validator["receipt_command"]
    assert "receipt_command" in validator["verdict"]
    assert "receipt_command" in validator["runtime_equivalence"]
    receipt_rules = [rule for rule in RULES.values() if rule["stage"] == "receipt"]
    assert receipt_rules, "no receipt-stage rule in the catalogue"
    for rule in receipt_rules:
        assert "--receipt" in rule["enforced_when"], rule["id"]


def test_command_line_follows_an_in_root_symlink_that_admission_itself_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADM-READ-REGULAR-FILE binds the path admission receives; main() resolves the path first."""
    data = yaml.safe_dump(_reviewed(), sort_keys=False).encode()
    link = _place(tmp_path / "packs" / TENANT, data, symlink=True)
    assert link.is_symlink()

    # The path as given: refused by the rule.
    results, code = validate_spec(link, complete=True, require_reviewed=True)
    assert code == 1
    assert re.search(RULES["ADM-READ-REGULAR-FILE"]["validator"]["message_pattern"], results[0]["message"])

    # The command line resolves it under --root, then admits the target's bytes.
    code, out = _main(
        [str(link), "--root", str(tmp_path), "--tenant-id", TENANT, "--complete", "--require-reviewed", "--json"],
        capsys,
    )
    assert (code, out["results"][0]["status"]) == (0, "PASS")
    assert Path(out["spec_path"]) == link.resolve()
    assert "command line" in RULES["ADM-READ-REGULAR-FILE"]["enforced_when"]


def test_command_line_refuses_a_symlink_that_leaves_the_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_dump(_reviewed(), sort_keys=False).encode()
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(data)
    root = tmp_path / "root"
    (root / TENANT).mkdir(parents=True)
    link = root / TENANT / "spec.yaml"
    link.symlink_to(outside)

    with pytest.raises(SystemExit) as exit_info:
        validate_domain.main([str(link), "--root", str(root), "--complete", "--json"])
    assert exit_info.value.code == 2
    assert "must stay under --root" in capsys.readouterr().err


def test_default_loader_symlink_boundary_matches_the_catalogue(tmp_path: Path) -> None:
    """A symlinked ancestor of the pack root is accepted; a symlinked spec.yaml inside it is refused."""
    data = yaml.safe_dump(_reviewed(), sort_keys=False).encode()
    _place(tmp_path / "actual" / "packs" / TENANT, data, symlink=False)
    alias = tmp_path / "authorized"
    alias.symlink_to(tmp_path / "actual", target_is_directory=True)
    assert isinstance(_load(alias / "packs", TENANT), DomainSpec)

    _place(tmp_path / "linked" / TENANT, data, symlink=True)
    refusal = _load(tmp_path / "linked", TENANT)
    assert isinstance(refusal, Exception)
    assert RULES["ADM-READ-REGULAR-FILE"]["runtime"]["default_loader"] == "refused"
    assert "symlinked ancestor of the pack root is accepted" in RULES["ADM-READ-REGULAR-FILE"]["enforced_when"]


def test_every_catalogue_rule_has_a_negative_case_and_every_case_names_a_rule() -> None:
    exercised = {case.rule for case in CASES}
    assert exercised - set(RULES) == set(), "negative parity case names an unknown rule id"
    assert set(RULES) - exercised == set(), "catalogue rule without a negative parity case"
    assert len({case.id for case in CASES}) == len(CASES)


def test_catalogue_shape_and_stable_ids() -> None:
    catalogue = admission_rules()
    ids = [rule["id"] for rule in catalogue["rules"]]
    assert catalogue["schema"] == "ceg.domain-admission-rules/v1"
    assert len(ids) == len(set(ids))
    for rule in catalogue["rules"]:
        assert re.fullmatch(r"ADM(-[A-Z0-9]+)+", rule["id"]), rule["id"]
        assert set(rule) == {
            "id",
            "stage",
            "statement",
            "enforced_by",
            "enforced_when",
            "validator",
            "runtime",
            "existing_proof",
        }
        assert rule["stage"] in catalogue["validator"]["stage_order"]
        assert rule["statement"].strip() == rule["statement"]
        assert "\n" not in rule["statement"]
        assert rule["enforced_by"]
        assert rule["validator"]["error_class"] in {"DomainSpecError", "DomainNotFoundError"}
        assert rule["runtime"]["default_loader"] in {"refused", "admitted"}
        re.compile(rule["validator"]["message_pattern"])


def _resolve(reference: str) -> object:
    path, _, qualified = reference.partition("::")
    assert (REPO / path).is_file(), reference
    target: object = importlib.import_module(path.removesuffix(".py").replace("/", "."))
    for part in qualified.split("."):
        target = getattr(target, part)
    return target


def test_every_enforced_by_reference_resolves_to_code() -> None:
    for rule in RULES.values():
        for reference in rule["enforced_by"]:
            assert _resolve(reference) is not None, f"{rule['id']}: {reference}"


def test_every_existing_proof_names_a_real_test() -> None:
    for rule in RULES.values():
        for reference in rule["existing_proof"]:
            path, _, qualified = reference.partition("::")
            source = (REPO / path).read_text(encoding="utf-8")
            for part in qualified.split("."):
                keyword = "class" if part.startswith("Test") else "def"
                assert re.search(rf"^\s*{keyword} {re.escape(part)}\b", source, re.MULTILINE), reference


# ── `--list-rules` ───────────────────────────────────────────────────────────


def test_list_rules_json_prints_the_committed_catalogue(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        validate_domain.main(["--list-rules", "--json"])

    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == json.loads(validate_domain.RULES_PATH.read_text(encoding="utf-8"))


def test_list_rules_text_names_every_rule(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        validate_domain.main(["--list-rules"])

    lines = capsys.readouterr().out.splitlines()
    assert exit_info.value.code == 0
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == list(RULES)
    assert lines[-1] == f"{len(RULES)} rules (ceg.domain-admission-rules/v1)"


def test_spec_path_is_still_required_without_list_rules(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        validate_domain.main([])

    assert exit_info.value.code == 2
    assert "spec_path" in capsys.readouterr().err
