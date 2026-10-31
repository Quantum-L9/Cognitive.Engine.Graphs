"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, l9-core, integrity]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_l9_core_models.py
RU-087 / RG-116: the l9_core envelope verifies a supplied content_hash and
detects payload mutation.

The envelope class is reached through ``make_root_packet`` (its declaration
module's own factory) rather than imported by name: the class is deprecated
in favour of TransportPacket and only its declaration site may name it.
"""

from __future__ import annotations

from typing import Any

import pytest

from l9_core.models import PacketLineage, TenantContext, make_root_packet


def _root(payload: dict[str, Any] | None = None) -> Any:
    return make_root_packet("match_request", tenant_id="t1", actor="gate", payload=payload or {"q": {"x": 1}})


def _fields(packet: Any) -> dict[str, Any]:
    return {
        "packet_id": packet.packet_id,
        "packet_type": packet.packet_type,
        "tenant": TenantContext(tenant_id=packet.tenant.tenant_id, actor=packet.tenant.actor),
        "payload": dict(packet.payload),
        "lineage": PacketLineage(**packet.lineage.model_dump()),
        "created_at": packet.created_at,
    }


@pytest.mark.unit
class TestContentHashVerification:
    def test_forged_hash_is_rejected(self) -> None:
        packet = _root()
        envelope = type(packet)
        with pytest.raises(ValueError, match="content_hash"):
            envelope(**_fields(packet), content_hash="0" * 64)

    def test_hash_computed_when_not_supplied(self) -> None:
        packet = _root()
        assert packet.content_hash == packet.compute_content_hash()
        assert len(packet.content_hash) == 64

    def test_matching_supplied_hash_is_kept(self) -> None:
        packet = _root()
        rebuilt = type(packet)(**_fields(packet), content_hash=packet.content_hash)
        assert rebuilt.content_hash == packet.content_hash

    def test_round_trip_through_model_dump(self) -> None:
        packet = _root()
        rebuilt = type(packet).model_validate(packet.model_dump())
        assert rebuilt.content_hash == packet.content_hash
        assert rebuilt.verify_integrity()


@pytest.mark.unit
class TestPayloadIntegrity:
    def test_caller_mutation_after_construction_does_not_reach_packet(self) -> None:
        payload = {"q": {"x": 1}}
        packet = _root(payload)
        payload["q"]["x"] = 2
        payload["extra"] = True
        assert packet.payload == {"q": {"x": 1}}
        assert packet.verify_integrity()

    def test_in_place_payload_mutation_is_detected(self) -> None:
        packet = _root()
        assert packet.verify_integrity()
        packet.payload["q"]["x"] = 99
        assert packet.verify_integrity() is False

    def test_derive_unchanged(self) -> None:
        packet = _root()
        child = packet.derive("match_response", {"ok": True})
        assert child.lineage.parent_id == packet.packet_id
        assert child.lineage.root_id == packet.lineage.root_id
        assert child.lineage.hop_count == 1
        assert child.content_hash == child.compute_content_hash()
        assert child.verify_integrity()
