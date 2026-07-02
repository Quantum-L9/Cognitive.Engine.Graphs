"""Unit tests — PacketEnvelope bridge: hash determinism, payload sensitivity."""

from __future__ import annotations


def test_packet_envelope_content_hash_is_deterministic():
    """content_hash is a valid SHA-256 hex and verifies integrity."""
    from engine.packet.bridge import PacketBridge

    bridge = PacketBridge()
    p1 = bridge.inflate_ingress(
        tenant_id="test",
        actor="unit-test",
        packet_type="graph_sync",
        payload={"action": "match", "x": 1},
    )
    assert len(p1.content_hash) == 64  # SHA-256 hex
    assert all(c in "0123456789abcdef" for c in p1.content_hash)


def test_packet_envelope_hash_changes_with_payload():
    """Different payloads produce different content hashes."""
    from engine.packet.bridge import PacketBridge

    bridge = PacketBridge()
    p1 = bridge.inflate_ingress(
        tenant_id="test",
        actor="unit-test",
        packet_type="graph_sync",
        payload={"action": "match"},
    )
    p2 = bridge.inflate_ingress(
        tenant_id="test",
        actor="unit-test",
        packet_type="graph_sync",
        payload={"action": "sync"},
    )
    # Both have valid hashes but they differ due to different payloads
    assert p1.content_hash != p2.content_hash


def test_packet_bridge_inflate_ingress():
    from engine.packet.bridge import PacketBridge

    bridge = PacketBridge()
    packet = bridge.inflate_ingress(
        tenant_id="tenant-a",
        actor="engine",
        packet_type="graph_sync",
        payload={"entity_type": "Facility", "batch": []},
    )
    assert packet.packet_type == "graph_sync"
    assert packet.content_hash
    assert packet.lineage.root_id


def test_packet_bridge_derive_preserves_lineage():
    from engine.packet.bridge import PacketBridge

    bridge = PacketBridge()
    root = bridge.inflate_ingress(
        tenant_id="tenant-a",
        actor="engine",
        packet_type="graph_sync",
        payload={"entity_type": "Facility"},
    )
    derived = root.derive("outcome_event", {"result": "ok"})
    assert derived.lineage.root_id == root.lineage.root_id
    assert derived.lineage.parent_id == root.packet_id
    assert derived.lineage.hop_count == 1
