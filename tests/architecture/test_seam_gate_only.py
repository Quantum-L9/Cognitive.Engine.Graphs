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
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
CHASSIS = ROOT / "chassis"

# `enrich` is owned by EIE in Gate's action-ownership map; CEG's local handler of
# the same name must never be advertised or accepted from Gate.
_NOT_ADVERTISED = {"enrich", "admin", "health", "healthcheck"}

# HEALTH-01 (operator decision 2026-10-22): the advertised set is exactly the
# three EIE->CEG seam actions, resolve, and the canonical health route. Health is
# not routed through `admin`.
_ADVERTISED_EXPECTED = {"match", "sync", "outcomes", "resolve", "health-assess", "score"}
_HEALTH_SUBACTIONS = ("health_assess", "health_batch_assess", "health_report")
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


# ── HEALTH-01: the canonical health route is `health-assess`, never `admin` ────


def test_spec_advertised_set_is_exact():
    advertised = set(_spec()["node"]["actions"])
    assert advertised == _ADVERTISED_EXPECTED, (
        f"advertised {sorted(advertised)} != {sorted(_ADVERTISED_EXPECTED)}: health is routed through "
        "`health-assess` (HEALTH-01); add or remove an advertised action here deliberately"
    )


@pytest.mark.parametrize("surface", ["admin", "health", "healthcheck"])
def test_spec_never_advertises_operator_surfaces(surface: str):
    """Gate must never be able to route `admin` (or the liveness probes) to CEG."""
    assert surface not in _spec()["node"]["actions"], f"{surface!r} is an operator/probe surface, not a Gate route"


def test_health_assess_is_its_own_handler_not_admin():
    from engine.handlers import ACTION_HANDLERS, handle_admin, handle_health_assess

    assert ACTION_HANDLERS["health-assess"] is handle_health_assess
    assert ACTION_HANDLERS["health-assess"] is not handle_admin
    assert "admin" in ACTION_HANDLERS, "admin stays registered for direct operator calls in dev"


def _admin_subactions_from_source() -> list[str]:
    """Every literal `subaction == "..."` branch of handle_admin, from the AST.

    The `subaction in {...}` health branch compares with `In`, not `Eq`, so the
    three health subactions are not part of this list by construction.
    """
    tree = ast.parse((ENGINE / "handlers.py").read_text(encoding="utf-8"))
    admin = next(fn for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "handle_admin")
    found: set[str] = set()
    for node in ast.walk(admin):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "subaction"):
            continue
        right = node.comparators[0]
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            found.add(right.value)
    return sorted(found)


_ADMIN_SUBACTIONS = _admin_subactions_from_source()


def test_admin_subaction_enumeration_is_meaningful():
    """Guard the parametrisation below against a silent empty list."""
    assert len(_ADMIN_SUBACTIONS) >= 20, _ADMIN_SUBACTIONS
    for must in ("list_domains", "init_schema", "trigger_gds", "erase_subject", "delegate_capability", "trigger_kge"):
        assert must in _ADMIN_SUBACTIONS
    assert not set(_ADMIN_SUBACTIONS) & set(_HEALTH_SUBACTIONS)


@pytest.fixture
def engine_up(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An initialised engine with no tenant allowlist and the health API on."""
    from engine.config.settings import Settings
    from engine.handlers import init_dependencies
    from engine.state import _reset_singleton

    monkeypatch.delenv("TENANT_ALLOWLIST", raising=False)
    _reset_singleton()
    init_dependencies(MagicMock(), MagicMock())
    settings = Settings(neo4j_password="test-pw", api_secret_key="test-key", health_api_enabled=True)
    try:
        with patch("engine.config.settings.settings", settings):
            yield
    finally:
        _reset_singleton()


@pytest.fixture
def health_handlers_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.health import api as health_api

    async def _must_not_run(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a health handler ran for a request health-assess must refuse")

    for name in ("handle_health_assess", "handle_health_batch_assess", "handle_health_report"):
        monkeypatch.setattr(health_api, name, _must_not_run)


@pytest.mark.parametrize("subaction", _ADMIN_SUBACTIONS)
async def test_health_assess_refuses_every_admin_subaction(
    subaction: str, engine_up: None, health_handlers_must_not_run: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rest of `admin` is unreachable through the canonical health route."""
    from engine import handlers

    async def _admin_must_not_run(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("health-assess must never delegate to handle_admin")

    monkeypatch.setattr(handlers, "handle_admin", _admin_must_not_run)
    with pytest.raises(handlers.ValidationError, match="is not a health subaction") as exc_info:
        await handlers.handle_health_assess("plasticos", {"subaction": subaction, "domain_id": "plasticos"})
    assert exc_info.value.action == "health-assess"


async def test_health_assess_requires_a_subaction(engine_up: None, health_handlers_must_not_run: None) -> None:
    from engine import handlers

    with pytest.raises(handlers.ValidationError, match="Missing required field 'subaction' in health-assess payload"):
        await handlers.handle_health_assess("plasticos", {})


@pytest.mark.parametrize("subaction", _HEALTH_SUBACTIONS)
async def test_health_assess_binds_domain_to_tenant(
    subaction: str, engine_up: None, health_handlers_must_not_run: None
) -> None:
    """HEALTH-05 on the canonical route: payload.domain must equal the packet tenant."""
    from engine import handlers

    with pytest.raises(handlers.ValidationError, match="does not match packet tenant") as exc_info:
        await handlers.handle_health_assess("plasticos", {"subaction": subaction, "domain": "other-tenant"})
    assert exc_info.value.action == "health-assess"


# ── Deployment allowlists: the SDK inbound policy accepts only L9_ALLOWED_ACTIONS ──
# A Gate-routed action absent from the list is refused before any handler runs,
# so every checked-in deployment must list exactly ACTION_HANDLERS minus `enrich`
# (what `make dev` derives) — in particular the canonical `health-assess` route.
# Operator approval 2026-10-22 covers these three protected files.

_ALLOWLIST_FILES = ("docker-compose.yml", "docker-compose.prod.yml", ".env.template")


def _allowed_actions_in(filename: str) -> set[str]:
    """The L9_ALLOWED_ACTIONS value a checked-in deployment file hands the SDK."""
    text = (ROOT / filename).read_text(encoding="utf-8")
    if filename.endswith(".yml"):
        services = yaml.safe_load(text)["services"]
        values = [
            svc["environment"]["L9_ALLOWED_ACTIONS"]
            for svc in services.values()
            if isinstance(svc.get("environment"), dict) and "L9_ALLOWED_ACTIONS" in svc["environment"]
        ]
    else:
        values = re.findall(r"^L9_ALLOWED_ACTIONS=(.*)$", text, flags=re.MULTILINE)
    assert len(values) == 1, f"{filename}: expected exactly one L9_ALLOWED_ACTIONS, found {values!r}"
    return {item.strip() for item in str(values[0]).split(",") if item.strip()}


@pytest.mark.parametrize("filename", _ALLOWLIST_FILES)
def test_deployment_allowlist_is_action_handlers_minus_enrich(filename: str):
    from engine.handlers import ACTION_HANDLERS

    allowed = _allowed_actions_in(filename)
    expected = set(ACTION_HANDLERS) - {"enrich"}
    assert allowed == expected, (
        f"{filename}: L9_ALLOWED_ACTIONS {sorted(allowed)} != ACTION_HANDLERS minus enrich {sorted(expected)}; "
        "an advertised action missing here is refused by the SDK inbound policy before its handler runs"
    )


@pytest.mark.parametrize("filename", _ALLOWLIST_FILES)
def test_deployment_allowlist_accepts_every_advertised_action(filename: str):
    advertised = set(_spec()["node"]["actions"])
    missing = sorted(advertised - _allowed_actions_in(filename))
    assert missing == [], f"{filename}: Gate may route {missing} to CEG but L9_ALLOWED_ACTIONS refuses them"
