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

Source-bound action schema tests. These parse production Python and the registry,
not README text or a separately maintained test copy of the permission map.
They are contract-shape tests; their passing does NOT establish handler-side
JSON Schema enforcement, tenant admission, or Gate runtime authorization.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from tests.contracts._constants import CONTRACTS_ROOT

REPO = Path(__file__).resolve().parents[2]
TOOL_NAMES = {"match", "sync", "admin", "outcomes", "resolve", "enrich"}


def _schema(name: str) -> dict:
    p = CONTRACTS_ROOT / "agents" / "tool-schemas" / f"{name}.schema.json"
    assert p.is_file(), p
    return json.loads(p.read_text(encoding="utf-8"))


def _tree(path: str) -> ast.Module:
    p = REPO / path
    assert p.is_file(), f"production source missing: {p}"
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _payload_keys(fn: ast.AST) -> set[str]:
    consumed = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "payload":
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                consumed.add(node.slice.value)
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "payload"
                and node.func.attr in {"get", "pop"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                consumed.add(node.args[0].value)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "_require_key"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "payload"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                consumed.add(node.args[1].value)
    return consumed


def _code_permission_map() -> dict[str, str]:
    """Resolve literal PERM_* constants and ACTION_PERMISSION_MAP from production AST.

    This avoids the previous false green where two manually copied contracts
    agreed with each other while neither was checked against real permissions.
    """
    tree = _tree("engine/auth/capabilities.py")
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value.value, str):
                    constants[target.id] = node.value.value
    amap = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "ACTION_PERMISSION_MAP"
    )
    assert isinstance(amap.value, ast.Dict)
    resolved = {}
    for key, value in zip(amap.value.keys, amap.value.values, strict=True):
        assert isinstance(key, ast.Constant)
        assert isinstance(key.value, str)
        if isinstance(value, ast.Name):
            resolved[key.value] = constants[value.id]
        elif isinstance(value, ast.Constant):
            resolved[key.value] = value.value
        else:
            raise AssertionError(f"Permission map gained dynamic initializer: {key.value}")
    return resolved


def _admin_subactions() -> set[str]:
    fn = _fn(_tree("engine/handlers.py"), "handle_admin")
    found = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if (
            isinstance(test.left, ast.Name)
            and test.left.id == "subaction"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and isinstance(test.comparators[0].value, str)
        ):
            found.add(test.comparators[0].value)
    # The three health branches are an explicit frozenset constant in handlers.py.
    for n in _tree("engine/handlers.py").body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "_HEALTH_SUBACTIONS":
            assert isinstance(n.value, ast.Call)
            assert isinstance(n.value.func, ast.Name)
            assert n.value.func.id == "frozenset"
            assert len(n.value.args) == 1
            found |= set(ast.literal_eval(n.value.args[0]))
            break
    return found


def _admin_condition_required(schema: dict, name: str) -> set[str]:
    for clause in schema.get("allOf", []):
        if clause.get("if", {}).get("properties", {}).get("subaction", {}).get("const") == name:
            return set(clause["then"]["required"])
    return set()


def test_index_matches_six_legacy_tools_and_runtime_handlers() -> None:
    idx = yaml.safe_load((CONTRACTS_ROOT / "agents/tool-schemas/_index.yaml").read_text())
    tools = idx["tools"]
    assert {i["name"] for i in tools} == TOOL_NAMES
    source = _tree("engine/handlers.py")
    mapping = next(
        n
        for n in source.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "ACTION_HANDLERS"
    )
    handlers = {k.value for k in mapping.value.keys if isinstance(k, ast.Constant)}
    assert handlers >= TOOL_NAMES
    permissions = _code_permission_map()
    assert set(permissions) >= TOOL_NAMES
    for i in tools:
        assert i["handler"] == f"engine/handlers.py:handle_{i['name']}"
        assert i["schema_file"] == f"{i['name']}.schema.json"
        assert _schema(i["name"])["x-required-capability-current"] == i["capability"] == permissions[i["name"]]


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_schema_draft_and_source_provenance(name: str) -> None:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["x-source-file"] == f"engine/handlers.py:handle_{name}"
    assert schema["$id"].endswith(f"/{name}.schema.json")
    assert schema["x-source-revision"]
    assert schema["x-runtime-validator-applies-this-json-schema"] is False
    assert "NOT runtime rejection proofs" in schema["x-runtime-validation-boundary"]
    assert schema["examples"]
    assert schema["x-negative-examples"]


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_positive_and_negative_instances(name: str) -> None:
    s = _schema(name)
    v = Draft202012Validator(s)
    for i, example in enumerate(s["examples"]):
        errors = list(v.iter_errors(example))
        assert not errors, f"{name} good[{i}]: {[e.message for e in errors]}"
    for i, example in enumerate(s["x-negative-examples"]):
        assert not v.is_valid(example), f"{name} bad[{i}] unexpectedly passed"
    assert not v.is_valid({**s["examples"][0], "__unapproved_contract_key__": True})


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_schema_covers_every_direct_payload_key_used_by_handler(name: str) -> None:
    fn = _fn(_tree("engine/handlers.py"), f"handle_{name}")
    consumed = _payload_keys(fn)
    if name == "admin":
        consumed.add("domain_id")  # indirect _bind_admin_domain
    missing = consumed - set(_schema(name)["properties"])
    assert not missing, f"{name}: source consumes keys absent from schema {sorted(missing)}"


def test_match_models_both_actual_source_paths() -> None:
    s = _schema("match")
    model = _tree("engine/models/payloads.py")
    match = next(n for n in model.body if isinstance(n, ast.ClassDef) and n.name == "MatchRequest")
    fields = {n.target.id for n in match.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    assert fields | {"domain"} <= set(s["properties"])
    conditional = s["allOf"][0]
    assert {
        "contract_version",
        "domain",
        "query_id",
        "direction",
        "query_entity_ref",
        "query",
        "top_n",
        "projection_version",
        "policy_ref",
    } <= set(conditional["then"]["required"])
    assert {"query", "match_direction"} <= set(conditional["else"]["required"])
    example = json.loads((REPO / "contracts/payloads/examples/match-request.json").read_text())
    assert Draft202012Validator(s).is_valid(example), "real typed MatchRequest fixture rejected"
    handlers_source = (REPO / "engine/handlers.py").read_text()
    assert "_match_request_as_legacy(tenant, payload, domain_loader)" in handlers_source
    assert "admit_payload(MatchRequest, payload, contract)" in handlers_source


def test_outcome_schema_contains_writer_effective_feedback_fields() -> None:
    s = _schema("outcomes")
    f = s["properties"]["fingerprint"]["properties"]
    helper = _fn(_tree("engine/handlers.py"), "_outcome_feedback_fields")
    for key in ("dimension_scores", "was_selected"):
        assert key in _payload_keys_from_name(helper, "fingerprint")
        assert key in f
    assert f["was_selected"]["type"] == ["boolean", "null"]
    writer = (REPO / "engine/handlers.py").read_text()
    assert "_outcome_feedback_fields(fingerprint) if unified else None" in writer


def _payload_keys_from_name(fn: ast.AST, name: str) -> set[str]:
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
            if n.func.value.id == name and n.func.attr == "get" and n.args and isinstance(n.args[0], ast.Constant):
                keys.add(n.args[0].value)
    return keys


def test_admin_enum_and_requirements_come_from_real_dispatch_branches() -> None:
    schema = _schema("admin")
    assert set(schema["properties"]["subaction"]["enum"]) == _admin_subactions()
    assert {"domain_id", "test_set"} <= _admin_condition_required(schema, "evaluate")
    assert {"entity_id", "node_label"} <= _admin_condition_required(schema, "health_assess")
    assert {"source_tenant", "target_tenant", "domain_id", "actions"} <= _admin_condition_required(
        schema, "delegate_capability"
    )
    evaluate = _fn(_tree("engine/handlers.py"), "_handle_evaluate")
    assert {"test_set", "domain_id"} <= _payload_keys(evaluate) | {"domain_id"}
    health_schemas = _tree("engine/health/health_schemas.py")
    req = next(n for n in health_schemas.body if isinstance(n, ast.ClassDef) and n.name == "HealthAssessmentRequest")
    mandatory = {
        n.target.id
        for n in req.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is None
    }
    assert mandatory <= _admin_condition_required(schema, "health_assess")


def test_gate_exposure_not_equivalent_to_sdk_handler_registration() -> None:
    spec = yaml.safe_load((REPO / "engine/spec.yaml").read_text())
    advertised = set(spec["node"]["actions"])
    assert {"match", "sync", "outcomes", "resolve"} <= advertised
    assert {"admin", "enrich"}.isdisjoint(advertised)
    # score is Gate-routed since #397 (REMOTE_SCORE_ENABLED, default off) but is
    # still not an agent tool: no tool schema is published for it.
    assert "score" in advertised
    assert "health-assess" in advertised
    assert "score" not in {
        x["name"] for x in yaml.safe_load((CONTRACTS_ROOT / "agents/tool-schemas/_index.yaml").read_text())["tools"]
    }
    registered = (REPO / "chassis/handler_registration.py").read_text()
    assert "for action, handler in ACTION_HANDLERS.items():" in registered
    for name in ("admin", "enrich"):
        assert _schema(name)["x-exposure"].startswith("local_")
    assert not (CONTRACTS_ROOT / "agents/tool-schemas/score.schema.json").exists()


def test_resolve_write_permission_and_fail_closed_behavior_are_source_aligned() -> None:
    src = (REPO / "engine/auth/capabilities.py").read_text()
    permissions = _code_permission_map()
    rbac = yaml.safe_load((CONTRACTS_ROOT / "auth/rbac-policy.yaml").read_text())
    assert rbac["action_permission_current"]["resolve"] == permissions["resolve"]
    assert rbac["inconsistency"]["declared_permission"] == permissions["resolve"]
    assert permissions["resolve"] == "sync:write"
    assert "return _is_read_capability(action)" in src
    assert rbac["release_ready"] is False
    assert rbac["inconsistency"]["status"] == "source_remediated_local"
    additional = {x["id"]: x["status"] for x in rbac["additional_inconsistencies"]}
    assert additional["AUTH-CAPSET-02"] == "source_remediated_local"
    assert additional["AUTH-GATE-03"] == "unverified"
    assert "write" in _schema("resolve")["x-side-effects"]


def test_source_permissions_are_bound_to_all_six_contracts() -> None:
    permissions = _code_permission_map()
    for name in TOOL_NAMES:
        assert _schema(name)["x-required-capability-current"] == permissions[name]


def test_contracts_do_not_claim_code_enforces_json_schema() -> None:
    for name in TOOL_NAMES:
        s = _schema(name)
        assert s["x-runtime-validator-applies-this-json-schema"] is False
        assert s["x-authorization-is-runtime-owned"] is True
        assert s["x-contract-status"] == "candidate_unenforced_not_release_admitted"


@pytest.mark.parametrize("expr", ["MATCH (n) DETACH DELETE n", "n.x; CALL db.labels()"])
def test_enrich_expression_injection_is_rejected_by_actual_handler_sanitizer(expr: str) -> None:
    """Contract shape alone cannot make a dynamic Cypher expression safe."""
    from engine.handlers import ValidationError, _sanitize_expression

    with pytest.raises(ValidationError):
        _sanitize_expression(expr)


def test_enrich_normal_expression_is_accepted_by_actual_handler_sanitizer() -> None:
    from engine.handlers import _sanitize_expression

    assert _sanitize_expression("n.score + 1") == "n.score + 1"


def test_resolve_write_requires_permission_stronger_than_match_read() -> None:
    """Resolve rejects match:read and requires the shared graph-write grant."""
    from engine.auth.capabilities import CapabilitySet, check_action_permission

    read_only = CapabilitySet([{"actions": ["match:read"], "allowed_subjects": ["tenant_a"]}])
    assert not check_action_permission("tenant_a", "resolve", read_only)
    write_authorized = CapabilitySet([{"actions": ["sync:write"], "allowed_subjects": ["tenant_a"]}])
    assert check_action_permission("tenant_a", "resolve", write_authorized)
