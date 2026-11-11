"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, packet]
owner: engine-team
status: active
--- /L9_META ---

Tests for engine.packet — the TransportPacket wire contract and its chassis bridge.

TransportPacket (constellation_node_sdk) is the only wire format. The deprecated
PacketEnvelope declarations (engine/packet/packet_envelope.py, l9_core/models.py)
were removed under Quantum-L9/Cognitive.Engine.Graphs#138; the invariants they
were tested for are asserted here against TransportPacket instead.

Covers:
- Immutability (frozen model, extra=forbid)
- Hash integrity + determinism (payload_hash, transport_hash)
- Derivation (lineage parent_id / root_id / generation, hash recompute)
- Hop trace (with_hop appends without touching the signed core)
- Delegation chain (single + stacked)
- Serialization roundtrip and tamper rejection
- Hash formula documented in PACKET_ENVELOPE_FIELDS.md matches the SDK
- Chassis bridge (inflate_ingress, deflate_egress, delegate_to_node)
- Tenant context (originator defaults, delegation preservation)
- Root factory (create_transport_packet) and the closed packet_type set
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from constellation_node_sdk import (
    TransportAddress,
    TransportIntegrityError,
    TransportPacket,
    create_transport_packet,
)
from constellation_node_sdk.transport.hashing import (
    canonical_json,
    compute_payload_hash,
    compute_transport_hash,
)
from constellation_node_sdk.transport.hop_trace import make_response_hop
from pydantic import ValidationError

from engine.packet.chassis_contract import deflate_egress, delegate_to_node, inflate_ingress

pytestmark = pytest.mark.unit

_DOC = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "PACKET_ENVELOPE_FIELDS.md"

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def base_packet() -> TransportPacket:
    return create_transport_packet(
        action="match",
        payload={"polymer": "HDPE", "min_volume_kg": 5000},
        tenant={"actor": "acme_recycling", "originator": "acme_recycling"},
        source_node="plasticos-engine",
        destination_node="match-engine",
        reply_to="plasticos-engine",
        classification="confidential",
        compliance_tags=("SOC2",),
        trace_id="abc123-trace",
        correlation_id="corr-001",
    )


def _root(*, action: str = "match", tenant: str = "t", payload: dict | None = None) -> TransportPacket:
    return create_transport_packet(
        action=action,
        payload={"k": "v"} if payload is None else payload,
        tenant=tenant,
        source_node="x",
        trace_id="t",
    )


# ── Immutability ──────────────────────────────────────────


class TestImmutability:
    def test_frozen_payload(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.payload = {"hacked": True}

    def test_frozen_tenant(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.tenant = base_packet.tenant

    def test_frozen_header(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.header = base_packet.header

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            TransportAddress(source_node="x", destination_node="y", reply_to="x", evil_field="y")

    def test_extra_top_level_field_rejected(self, base_packet):
        wire = base_packet.model_dump_json_dict()
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            TransportPacket.model_validate({**wire, "evil_field": "y"})

    def test_sub_object_frozen(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.address.source_node = "hijacked"

    def test_security_frozen(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.security.transport_hash = "tampered"

    def test_header_frozen(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.header.trace_id = "replaced"

    def test_lineage_frozen(self, base_packet):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            base_packet.lineage.generation = 999


# ── Integrity ─────────────────────────────────────────────


class TestIntegrity:
    def test_hashes_match(self, base_packet):
        assert base_packet.security.payload_hash == compute_payload_hash(base_packet.payload)
        assert base_packet.security.transport_hash == compute_transport_hash(base_packet)

    def test_hash_deterministic(self, base_packet):
        # An independently re-validated copy must hash identically.
        copy = TransportPacket.model_validate(base_packet.model_dump())
        assert copy is not base_packet
        assert compute_transport_hash(copy) == base_packet.security.transport_hash

    def test_different_payload_different_hash(self, base_packet):
        other = _root(payload={"polymer": "PP", "min_volume_kg": 999})
        assert base_packet.security.payload_hash != other.security.payload_hash
        assert base_packet.security.transport_hash != other.security.transport_hash

    def test_different_tenant_different_hash(self):
        p1 = _root(tenant="tenant_a")
        p2 = _root(tenant="tenant_b")
        assert p1.security.payload_hash == p2.security.payload_hash
        assert p1.security.transport_hash != p2.security.transport_hash

    def test_different_action_different_hash(self):
        p1 = _root(action="match")
        p2 = _root(action="sync")
        assert p1.security.transport_hash != p2.security.transport_hash

    def test_hashes_are_sha256_hex(self, base_packet):
        for digest in (base_packet.security.payload_hash, base_packet.security.transport_hash):
            assert len(digest) == 64
            assert all(c in "0123456789abcdef" for c in digest)

    def test_in_place_payload_mutation_is_detected(self):
        """RU-087 carried over: the payload dict is mutable, so an in-place edit
        cannot be prevented — but it can no longer validate as a packet."""
        packet = _root(payload={"q": {"x": 1}})
        packet.payload["q"]["x"] = 99
        dumped = packet.model_dump()
        with pytest.raises(TransportIntegrityError, match="payload_hash"):
            TransportPacket.model_validate(dumped)


# ── Derivation ────────────────────────────────────────────


class TestDerivation:
    def test_derive_creates_new_id(self, base_packet):
        child = base_packet.derive(payload={"enriched": True})
        assert child.header.packet_id != base_packet.header.packet_id

    def test_derive_sets_parent(self, base_packet):
        child = base_packet.derive(payload={"enriched": True})
        assert child.lineage.parent_id == base_packet.header.packet_id
        assert child.header.causation_id == base_packet.header.packet_id

    def test_root_packet_is_its_own_root(self, base_packet):
        assert base_packet.lineage.parent_id is None
        assert base_packet.lineage.generation == 0
        assert base_packet.lineage.root_id == base_packet.header.packet_id

    def test_derive_increments_generation(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        grandchild = child.derive(payload={"x": 2})
        assert child.lineage.generation == 1
        assert grandchild.lineage.generation == 2

    def test_derive_preserves_root(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        grandchild = child.derive(payload={"x": 2})
        assert grandchild.lineage.root_id == base_packet.header.packet_id

    def test_derive_preserves_trace(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        assert child.header.trace_id == base_packet.header.trace_id

    def test_derive_recomputes_hash(self, base_packet):
        child = base_packet.derive(payload={"different": True})
        assert child.security.payload_hash != base_packet.security.payload_hash
        assert child.security.transport_hash != base_packet.security.transport_hash
        assert TransportPacket.model_validate(child.model_dump()) == child

    def test_derive_preserves_classification(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        assert child.security.classification == base_packet.security.classification

    def test_derive_preserves_correlation_id(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        assert child.header.correlation_id == base_packet.header.correlation_id

    def test_derive_preserves_tenant(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        assert child.tenant == base_packet.tenant

    def test_derive_leaves_parent_untouched(self, base_packet):
        before = base_packet.model_dump()
        base_packet.derive(payload={"x": 1}, packet_type="response")
        assert base_packet.model_dump() == before


# ── Hop trace ─────────────────────────────────────────────


class TestHopTrace:
    def test_with_hop_appends_without_changing_signed_core(self, base_packet):
        hop = make_response_hop(
            packet=base_packet,
            node="match-engine",
            action=base_packet.header.action,
            status="completed",
            duration_ms=5,
        )
        hopped = base_packet.with_hop(hop)
        assert hopped.hop_trace == (*base_packet.hop_trace, hop)
        assert hopped.security.payload_hash == base_packet.security.payload_hash
        assert hopped.security.transport_hash == base_packet.security.transport_hash
        assert base_packet.hop_trace == ()

    def test_with_hop_rejects_foreign_packet_hop(self, base_packet):
        other = _root()
        hop = make_response_hop(
            packet=other,
            node="match-engine",
            action="match",
            status="completed",
            duration_ms=5,
        )
        with pytest.raises(ValueError, match="packet_id"):
            base_packet.with_hop(hop)


# ── Delegation ────────────────────────────────────────────


class TestDelegation:
    def test_delegate_creates_delegation_link(self):
        source = inflate_ingress(
            action="match",
            payload={"polymer": "HDPE"},
            tenant="acme_recycling",
            trace_id="tr-delegate",
        )
        delegated = delegate_to_node(
            source_packet=source,
            from_node="orchestrator",
            to_node="enrichment-api",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert len(delegated.delegation_chain) == 1
        link = delegated.delegation_chain[0]
        assert link.delegator == "orchestrator"
        assert link.delegatee == "enrichment-api"
        assert link.scope == ("enrich",)

    def test_delegate_sets_on_behalf_of(self):
        source = inflate_ingress(
            action="match",
            payload={},
            tenant="acme_recycling",
            trace_id="tr-behalf",
        )
        delegated = delegate_to_node(
            source_packet=source,
            from_node="orchestrator",
            to_node="enrichment-api",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert delegated.tenant.on_behalf_of == "acme_recycling"

    def test_delegate_adds_hop(self):
        source = inflate_ingress(
            action="match",
            payload={},
            tenant="acme_recycling",
            trace_id="tr-hop-del",
        )
        delegated = delegate_to_node(
            source_packet=source,
            from_node="orchestrator",
            to_node="enrichment-api",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert len(delegated.hop_trace) == 1
        assert delegated.hop_trace[0].node == "orchestrator"
        assert delegated.hop_trace[0].status == "delegated"

    def test_stacked_delegation(self):
        source = inflate_ingress(
            action="match",
            payload={},
            tenant="acme_recycling",
            trace_id="tr-stack",
        )
        d1 = delegate_to_node(
            source_packet=source,
            from_node="agent",
            to_node="plasticos",
            delegated_action="match",
            scope=("match",),
        )
        d2 = delegate_to_node(
            source_packet=d1,
            from_node="plasticos",
            to_node="enrichment",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert len(d2.delegation_chain) == 2
        assert len(d2.hop_trace) >= 1
        assert d2.lineage.generation == 2
        assert d2.lineage.root_id == source.lineage.root_id

    def test_delegation_sets_audit_required(self):
        source = inflate_ingress(
            action="match",
            payload={},
            tenant="acme",
            trace_id="tr-audit",
        )
        delegated = delegate_to_node(
            source_packet=source,
            from_node="a",
            to_node="b",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert delegated.governance.audit_required is True


# ── Serialization ─────────────────────────────────────────


class TestSerialization:
    def test_roundtrip(self, base_packet):
        restored = TransportPacket.model_validate(base_packet.model_dump_json_dict())
        assert restored.header.packet_id == base_packet.header.packet_id
        assert restored.payload == base_packet.payload
        assert restored.security.transport_hash == base_packet.security.transport_hash

    def test_wire_is_json_serializable(self, base_packet):
        assert isinstance(json.dumps(base_packet.model_dump_json_dict()), str)

    def test_roundtrip_preserves_lineage(self, base_packet):
        child = base_packet.derive(payload={"x": 1})
        restored = TransportPacket.model_validate(child.model_dump_json_dict())
        assert restored.lineage.generation == 1
        assert restored.lineage.parent_id == base_packet.header.packet_id

    def test_roundtrip_preserves_delegation_chain(self):
        source = inflate_ingress(
            action="match",
            payload={},
            tenant="acme",
            trace_id="tr-wire-del",
        )
        delegated = delegate_to_node(
            source_packet=source,
            from_node="a",
            to_node="b",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert len(delegated.delegation_chain) == 1
        restored = type(delegated).model_validate_json(delegated.model_dump_json())
        assert len(restored.delegation_chain) == 1

    def test_roundtrip_and_derive_preserve_pii_fields(self, base_packet):
        # pii_fields lives in security outside transport_hash, so it is set on the wire form.
        wire = base_packet.model_dump_json_dict()
        wire["security"] = {**wire["security"], "pii_fields": ["contact_email"]}
        restored = TransportPacket.model_validate(wire)
        assert "contact_email" in restored.security.pii_fields
        assert "contact_email" in restored.derive(payload={"x": 1}).security.pii_fields

    def test_rejects_tampered_payload(self, base_packet):
        """RG-121 (RU-092 / CEG-COR-173) carried over: a tampered payload is rejected."""
        wire = base_packet.model_dump_json_dict()
        wire["payload"] = {**wire["payload"], "tampered": True}
        with pytest.raises(TransportIntegrityError, match="payload_hash"):
            TransportPacket.model_validate(wire)

    def test_rejects_tampered_tenant(self, base_packet):
        wire = base_packet.model_dump_json_dict()
        wire["tenant"] = {**wire["tenant"], "actor": "someone-else"}
        with pytest.raises(TransportIntegrityError, match="transport_hash"):
            TransportPacket.model_validate(wire)

    def test_rejects_forged_transport_hash(self, base_packet):
        wire = base_packet.model_dump_json_dict()
        wire["security"] = {**wire["security"], "transport_hash": "0" * 64}
        with pytest.raises(TransportIntegrityError, match="transport_hash"):
            TransportPacket.model_validate(wire)

    def test_rejects_forged_payload_hash(self, base_packet):
        wire = base_packet.model_dump_json_dict()
        wire["security"] = {**wire["security"], "payload_hash": "0" * 64}
        with pytest.raises(TransportIntegrityError, match="payload_hash"):
            TransportPacket.model_validate(wire)


def _doc_hash_block() -> str:
    section = _DOC.read_text(encoding="utf-8").split("## Hash Computation", 1)[1].split("\n## ", 1)[0]
    return section.split("```python", 1)[1].split("```", 1)[0]


class TestHashDocMatchesCode:
    """RG-121 / DEC-34 (align doc to code): PACKET_ENVELOPE_FIELDS.md names the
    inputs of the SDK's compute_transport_hash, exactly."""

    def test_doc_transport_hash_keys_equal_sdk_inputs(self):
        doc_keys = set(re.findall(r'^\s*"(\w+)":', _doc_hash_block(), re.MULTILINE))
        sdk_keys = set(re.findall(r'^\s*"(\w+)":', inspect.getsource(compute_transport_hash), re.MULTILINE))
        assert sdk_keys
        assert doc_keys == sdk_keys
        assert "hop_trace" not in doc_keys
        assert "signature" not in doc_keys

    def test_doc_formula_reproduces_sdk_hashes(self, base_packet):
        packet = base_packet
        payload_hash = hashlib.sha256(canonical_json(packet.payload)).hexdigest()
        transport_hash = hashlib.sha256(
            canonical_json(
                {
                    "header": packet.header,
                    "address": packet.address,
                    "tenant": packet.tenant,
                    "payload": packet.payload,
                    "governance": packet.governance,
                    "delegation_chain": packet.delegation_chain,
                    "lineage": packet.lineage,
                    "attachments": packet.attachments,
                    "provenance": packet.provenance,
                    "payload_hash": packet.security.payload_hash,
                }
            )
        ).hexdigest()
        assert payload_hash == packet.security.payload_hash
        assert transport_hash == packet.security.transport_hash


# ── Chassis Bridge ────────────────────────────────────────


class TestChassisBridge:
    def test_inflate(self):
        pkt = inflate_ingress(
            action="match",
            payload={"q": "test"},
            tenant="acme",
            trace_id="tr-1",
        )
        assert pkt.header.packet_type == "request"
        assert pkt.header.action == "match"
        assert pkt.tenant.actor == "acme"
        assert pkt.security.payload_hash

    def test_inflate_with_delegation(self):
        pkt = inflate_ingress(
            action="enrich",
            payload={"data": "x"},
            tenant="child_tenant",
            trace_id="tr-2",
            on_behalf_of="parent_tenant",
        )
        assert pkt.tenant.on_behalf_of == "parent_tenant"

    def test_deflate(self):
        request = inflate_ingress(
            action="match",
            payload={"polymer": "HDPE"},
            tenant="acme",
            trace_id="tr-deflate",
        )
        resp = deflate_egress(
            request=request,
            engine_data={"matches": [1, 2, 3]},
            processing_ms=42.5,
            engine_version="1.0.0",
            responding_node="match-engine",
        )
        assert resp.header.packet_type == "response"
        assert resp.payload["status"] == "success"
        assert resp.lineage.parent_id == request.header.packet_id
        assert resp.security.payload_hash

    def test_deflate_adds_hop(self):
        request = inflate_ingress(
            action="match",
            payload={},
            tenant="acme",
            trace_id="tr-hop",
        )
        resp = deflate_egress(
            request=request,
            engine_data={},
            processing_ms=10.0,
            engine_version="1.0.0",
            responding_node="match-engine",
        )
        assert len(resp.hop_trace) == 1
        assert resp.hop_trace[0].node == "match-engine"

    def test_deflate_includes_execution_meta(self):
        request = inflate_ingress(
            action="match",
            payload={},
            tenant="acme",
            trace_id="tr-meta",
        )
        resp = deflate_egress(
            request=request,
            engine_data={},
            processing_ms=55.3,
            engine_version="2.1.0",
            responding_node="engine-a",
        )
        meta = resp.payload["meta"]
        assert meta["execution_ms"] == 55.3
        assert meta["version"] == "2.1.0"


# ── Tenant Context ────────────────────────────────────────


class TestTenantContext:
    def test_originator_defaults_to_actor(self):
        assert _root(tenant="t1").tenant.originator == "t1"
        assert inflate_ingress(action="query", payload={}, tenant="t1", trace_id="t").tenant.originator == "t1"

    def test_multi_tenant_delegation_preserves_originator(self):
        source = inflate_ingress(
            action="match",
            payload={},
            tenant="acme_recycling",
            trace_id="tr-originator",
        )
        d = delegate_to_node(
            source_packet=source,
            from_node="agent",
            to_node="other-engine",
            delegated_action="enrich",
            scope=("enrich",),
        )
        assert d.tenant.originator == "acme_recycling"
        assert d.tenant.on_behalf_of == "acme_recycling"

    def test_org_and_user_passthrough(self):
        pkt = inflate_ingress(
            action="match",
            payload={},
            tenant="t1",
            trace_id="t",
            org_id="org-123",
            user_id="user-456",
        )
        assert pkt.tenant.org_id == "org-123"
        assert pkt.tenant.user_id == "user-456"


# ── Root factory ──────────────────────────────────────────


class TestFactory:
    def test_create_transport_packet_minimal(self):
        pkt = create_transport_packet(action="health", payload={}, tenant="system", source_node="monitor")
        assert pkt.header.packet_type == "request"
        assert pkt.header.action == "health"
        assert pkt.governance.intent == "health"
        assert TransportPacket.model_validate(pkt.model_dump()) == pkt

    def test_action_is_normalized(self):
        assert _root(action="  MATCH ").header.action == "match"

    def test_expires_at_set_and_inherited(self):
        expires = datetime.now(tz=UTC) + timedelta(days=30)
        pkt = create_transport_packet(action="match", payload={}, tenant="t", expires_at=expires)
        assert pkt.header.expires_at == expires
        assert pkt.derive(payload={"x": 1}).header.expires_at == expires

    def test_schema_version(self, base_packet):
        assert base_packet.header.schema_version == "1.0"


# ── packet_type set ───────────────────────────────────────


class TestPacketTypes:
    @pytest.mark.parametrize("packet_type", ["request", "response", "delegation", "event"])
    def test_packet_types_ceg_emits_are_accepted(self, base_packet, packet_type):
        assert base_packet.derive(packet_type=packet_type).header.packet_type == packet_type

    def test_unknown_packet_type_rejected(self, base_packet):
        with pytest.raises(ValidationError, match="packet_type must be one of"):
            base_packet.derive(packet_type="api_request")

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError, match="action must be lowercase"):
            _root(action="bad action!")
