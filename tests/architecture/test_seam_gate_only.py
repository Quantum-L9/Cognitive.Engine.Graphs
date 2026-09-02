"""Seam architecture guards (forensic audit 2026-09-02): CEG <-> Gate <-> EIE.

Locks the CEG side of the bidirectional seam:
  * the node advertises only CEG-owned, implemented actions (never `enrich`);
  * the SDK TransportPacket chassis is the default and legacy is dev/test only;
  * every outbound packet CEG authors is addressed to Gate;
  * no peer URL awareness and no raw HTTP transport to a peer outside the SDK;
  * the reverse path (CEG -> Gate -> EIE `enrich`) is wired, not dormant.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
CHASSIS = ROOT / "chassis"

# `enrich` is owned by EIE in Gate's action-ownership map; CEG's local handler of
# the same name must never be advertised or accepted from Gate.
_NOT_ADVERTISED = {"enrich", "admin", "health", "healthcheck"}
_PEER_ENV_NAMES = ("EIE_URL", "EIE_BASE_URL", "ENRICHMENT_URL", "ENRICHMENT_ENGINE_URL", "PEER_URL", "NODE_URL")


def _spec() -> dict:
    return yaml.safe_load((ENGINE / "spec.yaml").read_text(encoding="utf-8"))


def _production_sources() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for base in (ENGINE, CHASSIS):
        for path in sorted(base.rglob("*.py")):
            out.append((path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")))
    return out


def test_spec_node_identity_is_graph():
    node = _spec()["node"]
    assert node["id"] == "graph", "Gate's node registry and ownership aliases key CEG under node id 'graph'"
    assert node["health_endpoint"] == "/v1/health"


def test_spec_advertises_only_implemented_ceg_owned_actions():
    from engine.handlers import ACTION_HANDLERS

    advertised = list(_spec()["node"]["actions"])
    assert advertised, "spec.yaml must advertise at least one action"
    assert len(set(advertised)) == len(advertised), "duplicate advertised actions"
    unimplemented = sorted(set(advertised) - set(ACTION_HANDLERS))
    assert unimplemented == [], f"spec.yaml advertises actions with no handler: {unimplemented}"
    forbidden = sorted(set(advertised) & _NOT_ADVERTISED)
    assert forbidden == [], f"spec.yaml must not advertise {forbidden} (EIE-owned name or operator surface)"
    assert {"match", "sync", "outcomes"} <= set(advertised), "the EIE->CEG seam actions must be advertised"


def test_no_phantom_graph_prefixed_actions():
    """The 23 `graph-*` names were never implemented and made routing readiness a lie."""
    advertised = _spec()["node"]["actions"]
    assert not any(a.startswith("graph-") for a in advertised)


@pytest.mark.parametrize("path", ["docker-compose.yml", "docker-compose.prod.yml", ".env.template"])
def test_allowed_actions_never_include_enrich(path: str):
    text = (ROOT / path).read_text(encoding="utf-8")
    for match in re.finditer(r"L9_ALLOWED_ACTIONS[:=]\s*\"?([^\"\n]+)", text):
        actions = {a.strip() for a in match.group(1).split(",")}
        assert "enrich" not in actions, f"{path}: CEG must not accept `enrich` (Gate owns it for EIE)"


def test_sdk_chassis_is_default_and_legacy_is_dev_only():
    from chassis.entrypoint import DEFAULT_CHASSIS, LEGACY_PERMITTED_ENVS, SDK
    from engine.config.settings import Settings

    assert DEFAULT_CHASSIS == SDK
    assert "prod" not in LEGACY_PERMITTED_ENVS
    assert "staging" not in LEGACY_PERMITTED_ENVS
    assert Settings(l9_env="dev").require_sdk_chassis_in_prod is True


@pytest.mark.parametrize("path", ["docker-compose.yml", "docker-compose.prod.yml", ".env.template"])
def test_deployment_surfaces_select_sdk_chassis(path: str):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert re.search(r"L9_CHASSIS[:=]\s*sdk\b", text), f"{path} must select the SDK chassis"
    assert not re.search(r"L9_CHASSIS[:=]\s*legacy\b", text), f"{path} selects the legacy side-door chassis"


def test_production_compose_requires_gate_and_signing():
    text = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    for name in ("GATE_URL", "GATE_ADMIN_TOKEN", "L9_SIGNING_KEY"):
        assert re.search(rf"{name}: \$\{{{name}:\?", text), f"{name} must be required (no default) in prod compose"
    assert 'L9_REQUIRE_SIGNATURE: "true"' in text
    assert 'L9_ENFORCE_GATE_ONLY_INGRESS: "true"' in text


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _is_gate(expr: ast.expr | None, *, param_defaults: dict[str, ast.expr]) -> bool:
    if isinstance(expr, ast.Constant):
        return expr.value == "gate"
    if isinstance(expr, ast.Name):
        if expr.id in {"_GATE_NODE", "GATE_NODE"}:
            return True
        default = param_defaults.get(expr.id)
        return default is not None and _is_gate(default, param_defaults={})
    return False


def test_every_authored_transport_packet_targets_gate():
    """Every packet CEG authors is addressed to Gate.

    The one legitimate non-Gate destination is a packet addressed to this node
    itself (destination_node == "graph"): the audit reconstruction of an inbound
    request. A packet CEG addresses to itself cannot be egress, so it is exempt.
    """
    offenders: list[str] = []
    for rel, src in _production_sources():
        if "create_transport_packet" not in src:
            continue
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = fn.args
            defaults = dict(zip([a.arg for a in args.args][-len(args.defaults) :], args.defaults, strict=False))
            defaults.update(
                {a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=False) if d is not None}
            )
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name != "create_transport_packet":
                    continue
                dest = _kw(node, "destination_node")
                if _is_gate(dest, param_defaults=defaults):
                    continue
                if isinstance(dest, ast.Constant) and dest.value == "graph":
                    continue  # addressed to this node itself: inbound audit reconstruction, not egress
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"TransportPackets not addressed to Gate: {offenders}"


def test_packet_bridge_defaults_to_gate_destination():
    from engine.packet_bridge import build_request_packet

    packet = build_request_packet(action="match", payload={}, tenant="t", trace_id="trace-1")
    assert packet.address.destination_node == "gate"
    assert packet.address.source_node == "graph"


def test_no_peer_url_awareness_in_production_code():
    offenders = [
        f"{rel}: {name}"
        for rel, src in _production_sources()
        for name in _PEER_ENV_NAMES
        if re.search(rf"\b{name}\b", src)
    ]
    assert offenders == [], f"peer URL awareness found (Gate is the only routing authority): {offenders}"


def test_no_raw_http_transport_to_execute_outside_sdk():
    offenders = [
        rel
        for rel, src in _production_sources()
        if re.search(r"httpx\.(Async)?Client|requests\.(post|get|Session)|aiohttp\.ClientSession", src)
        and re.search(r"/v1/(execute|admin/register)", src)
    ]
    assert offenders == [], f"raw HTTP transport to a Gate/peer endpoint outside the SDK: {offenders}"


def test_reverse_path_is_wired_through_gate_egress():
    """CEG -> Gate -> EIE: the health trigger must actually send via engine.gate_egress."""
    src = (ENGINE / "health" / "enrichment_trigger.py").read_text(encoding="utf-8")
    assert "from engine.gate_egress import request_enrichment" in src
    assert re.search(r"await request_enrichment\(", src)
    egress = (ENGINE / "gate_egress.py").read_text(encoding="utf-8")
    assert "get_gate_client()" in egress
    assert 'ENRICH_ACTION = "enrich"' in egress
    assert "GateClientError" in egress, "SDK errors must be reported, not swallowed"


def test_gate_client_singleton_is_the_only_gateclient_constructor():
    sites = sorted(rel for rel, src in _production_sources() if re.search(r"\bGateClient\s*\(", src))
    assert sites == ["engine/gate_client.py"], f"GateClient constructed outside the singleton: {sites}"
