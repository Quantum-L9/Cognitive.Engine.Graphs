"""
AsyncAPI contract tests.

Sources:
  constellation_node_sdk TransportPacket (the wire model; TransportHeader.packet_type
    is a closed set)
  engine/packet/chassis_contract.py:inflate_ingress, deflate_egress
  chassis/actions.py:execute_action() — request -> persist lifecycle

The channel payloads must describe the SDK TransportPacket, not the retired flat
PacketEnvelope. The drift tests compare components.schemas against
TransportPacket.model_json_schema(), and the round-trip tests validate packets
built by the real inflate/deflate path against the message schemas.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from constellation_node_sdk import TransportHeader, TransportPacket

# The SDK keeps both vocabularies private; reading them (rather than copying them)
# is what makes these tests fail when Gate_SDK changes the wire contract.
from constellation_node_sdk.transport.models import _ACTION_RE as _SDK_ACTION_RE
from constellation_node_sdk.transport.models import _ALLOWED_PACKET_TYPES as _SDK_PACKET_TYPES

from engine.packet.chassis_contract import deflate_egress, inflate_ingress
from tests.contracts._constants import KNOWN_ACTIONS

jsonschema = pytest.importorskip("jsonschema")


def _message_payload(spec: dict[str, Any], channel: str) -> dict[str, Any]:
    msg = next(iter(spec["channels"][channel]["messages"].values()))
    return msg["payload"]


def _narrowing(spec: dict[str, Any], channel: str) -> dict[str, Any]:
    """The channel-specific allOf branch layered over the TransportPacket $ref."""
    branches = _message_payload(spec, channel)["allOf"]
    assert branches[0] == {"$ref": "#/components/schemas/TransportPacket"}
    return branches[1]


def _validator(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    # AsyncAPI 3.0's default schema format is a Draft 07 superset; refs resolve
    # against a root that carries the spec's components.
    root = {**schema, "components": spec["components"]}
    return jsonschema.Draft7Validator(root)


def _sdk_schemas() -> dict[str, dict[str, Any]]:
    raw = TransportPacket.model_json_schema()
    defs = raw.pop("$defs")
    return {**defs, "TransportPacket": raw}


def _is_refinement(generated: Any, documented: Any, path: str) -> None:
    """Assert `documented` is `generated` (SDK) with only constraints added."""
    if isinstance(generated, dict):
        assert isinstance(documented, dict), f"{path}: expected object"
        if path.endswith(".properties"):
            extra = set(documented) - set(generated)
            assert not extra, f"{path}: documents fields the SDK model does not have: {extra}"
        for key, value in generated.items():
            if key == "title":
                continue
            assert key in documented, f"{path}: missing {key!r} present in SDK schema"
            if key == "$ref":
                expected = value.replace("#/$defs/", "#/components/schemas/")
                assert documented[key] == expected, f"{path}.$ref: {documented[key]} != {expected}"
            else:
                _is_refinement(value, documented[key], f"{path}.{key}")
    elif isinstance(generated, list):
        assert isinstance(documented, list), f"{path}: expected list"
        assert len(generated) == len(documented), f"{path}: length differs from SDK schema"
        for i, (g, d) in enumerate(zip(generated, documented, strict=True)):
            _is_refinement(g, d, f"{path}[{i}]")
    else:
        assert generated == documented, f"{path}: {documented!r} != SDK {generated!r}"


@pytest.fixture
def request_packet() -> Any:
    return inflate_ingress(
        action="match",
        payload={"query": {"material_type": "HDPE"}},
        tenant="plasticos",
        trace_id="trace-asyncapi-001",
    )


@pytest.fixture
def response_packet(request_packet: Any) -> Any:
    return deflate_egress(
        request=request_packet,
        engine_data={"candidates": []},
        processing_ms=1.5,
        responding_node="graph-engine",
    )


def _wire(packet: Any) -> dict[str, Any]:
    return packet.model_dump_json_dict()


def test_asyncapi_version(asyncapi_spec):
    assert asyncapi_spec.get("asyncapi") == "3.0.0"


def test_asyncapi_has_info(asyncapi_spec):
    assert "info" in asyncapi_spec
    assert asyncapi_spec["info"].get("title")
    assert asyncapi_spec["info"].get("version")


def test_asyncapi_has_channels(asyncapi_spec):
    assert "channels" in asyncapi_spec
    assert len(asyncapi_spec["channels"]) > 0


def test_ingress_channel_defined(asyncapi_spec):
    assert "chassis.request.ingress" in asyncapi_spec["channels"]


def test_egress_channel_defined(asyncapi_spec):
    assert "chassis.response.egress" in asyncapi_spec["channels"]


def test_audit_persist_channel_defined(asyncapi_spec):
    assert "engine.audit.persist" in asyncapi_spec["channels"]


def test_ingress_channel_has_message(asyncapi_spec):
    ch = asyncapi_spec["channels"]["chassis.request.ingress"]
    assert ch.get("messages")


def test_ingress_message_action_enum_coverage(asyncapi_spec):
    header = _narrowing(asyncapi_spec, "chassis.request.ingress")["properties"]["header"]
    enum_vals = set(header["properties"]["action"]["enum"])
    missing = KNOWN_ACTIONS - enum_vals
    assert not missing, f"asyncapi.yaml ingress header.action enum missing: {missing}"
    bad = {a for a in enum_vals if not _SDK_ACTION_RE.match(a)}
    assert not bad, f"ingress actions violate the SDK header.action pattern: {bad}"


def test_asyncapi_no_external_broker_note(asyncapi_spec):
    info_desc = asyncapi_spec.get("info", {}).get("description", "")
    assert (
        "broker" in info_desc.lower()
        or "synchronous" in info_desc.lower()
        or "no external" in info_desc.lower()
        or "not use" in info_desc.lower()
    )


# ── Drift against the SDK model ─────────────────────────────────


def test_components_cover_every_sdk_model(asyncapi_spec):
    documented = set(asyncapi_spec["components"]["schemas"])
    assert documented == set(_sdk_schemas())


@pytest.mark.parametrize("model", sorted(_sdk_schemas()))
def test_component_schema_refines_sdk_model(asyncapi_spec, model):
    documented = asyncapi_spec["components"]["schemas"][model]
    _is_refinement(_sdk_schemas()[model], documented, model)


def test_header_packet_type_enum_is_sdk_closed_set(asyncapi_spec):
    header = asyncapi_spec["components"]["schemas"]["TransportHeader"]
    assert set(header["properties"]["packet_type"]["enum"]) == _SDK_PACKET_TYPES


def test_header_packet_type_enum_accepted_by_sdk(asyncapi_spec):
    enum_vals = asyncapi_spec["components"]["schemas"]["TransportHeader"]["properties"]["packet_type"]["enum"]
    for value in enum_vals:
        assert TransportHeader(packet_type=value, action="match").packet_type == value
    with pytest.raises(ValueError, match="packet_type"):
        TransportHeader(packet_type="api_request", action="match")


def test_header_action_pattern_matches_sdk(asyncapi_spec):
    header = asyncapi_spec["components"]["schemas"]["TransportHeader"]
    assert header["properties"]["action"]["pattern"] == _SDK_ACTION_RE.pattern


# ── Real packets validate; the retired flat envelope does not ───


def test_inflate_ingress_packet_matches_ingress_message(asyncapi_spec, request_packet):
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "chassis.request.ingress"))
    validator.validate(_wire(request_packet))


def test_deflate_egress_packet_matches_egress_message(asyncapi_spec, response_packet):
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "chassis.response.egress"))
    validator.validate(_wire(response_packet))


def test_request_packet_rejected_on_egress(asyncapi_spec, request_packet):
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "chassis.response.egress"))
    assert not validator.is_valid(_wire(request_packet))


def test_audit_persist_pair_validates(asyncapi_spec, request_packet, response_packet):
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "engine.audit.persist"))
    validator.validate({"request_packet": _wire(request_packet), "response_packet": _wire(response_packet)})


def test_retired_flat_envelope_rejected(asyncapi_spec):
    flat = {
        "packet_id": "550e8400-e29b-41d4-a716-446655440000",
        "packet_type": "REQUEST",
        "action": "match",
        "tenant": "plasticos",
        "payload": {},
        "content_hash": "0" * 64,
    }
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "chassis.request.ingress"))
    assert not validator.is_valid(flat)


def test_uppercase_packet_type_rejected(asyncapi_spec, request_packet):
    wire = _wire(request_packet)
    wire["header"]["packet_type"] = "REQUEST"
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "chassis.request.ingress"))
    assert not validator.is_valid(wire)


def test_non_hex_payload_hash_rejected(asyncapi_spec, request_packet):
    wire = _wire(request_packet)
    wire["security"]["payload_hash"] = "Z" * 64
    validator = _validator(asyncapi_spec, _message_payload(asyncapi_spec, "chassis.request.ingress"))
    assert not validator.is_valid(wire)


def test_packet_type_registry_doc_lists_sdk_closed_set(contracts_root):
    """docs/contracts/PACKET_TYPE_REGISTRY.md tables exactly the SDK packet_type set."""
    text = (contracts_root / "PACKET_TYPE_REGISTRY.md").read_text()
    section = text.split("## Current Registry", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE)
    assert set(rows) == _SDK_PACKET_TYPES
    assert len(rows) == len(_SDK_PACKET_TYPES)
