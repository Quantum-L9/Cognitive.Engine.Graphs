"""Unit tests — TransportPacket bridge: hash determinism, payload sensitivity.

Note: These tests require chassis integration modules that may not be implemented.
Tests skip gracefully if required modules are not available.
"""

from __future__ import annotations

import pytest


def test_transport_packet_payload_hash_is_deterministic():
    """TransportPacket payload hash is deterministic for same payload."""
    try:
        from constellation_node_sdk import create_transport_packet
    except ImportError:
        pytest.skip("constellation_node_sdk not installed")
    p1 = create_transport_packet(
        action="match",
        payload={"action": "match", "x": 1},
        tenant="test",
    )
    p2 = create_transport_packet(
        action="match",
        payload={"action": "match", "x": 1},
        tenant="test",
    )
    assert p1.security.payload_hash == p2.security.payload_hash
    assert len(p1.security.payload_hash) == 64  # SHA-256 hex


def test_transport_packet_hash_changes_with_payload():
    """TransportPacket payload hash changes when payload differs."""
    try:
        from constellation_node_sdk import create_transport_packet
    except ImportError:
        pytest.skip("constellation_node_sdk not installed")
    p1 = create_transport_packet(action="match", payload={"action": "match"}, tenant="test")
    p2 = create_transport_packet(action="sync", payload={"action": "sync"}, tenant="test")
    assert p1.security.payload_hash != p2.security.payload_hash


def test_packet_bridge_inflate_ingress():
    """PacketBridge.inflate_ingress creates valid packet."""
    try:
        from engine.packet.bridge import PacketBridge
    except ImportError:
        pytest.skip("engine.packet.bridge not implemented")
    bridge = PacketBridge()
    packet = bridge.inflate_ingress(
        tenant_id="tenant-a",
        actor="engine",
        packet_type="graph_sync",
        payload={"entity_type": "Facility", "batch": []},
    )
    assert packet.header.action == "graph_sync"
    assert packet.header.packet_type == "request"
    assert packet.security.payload_hash
    assert packet.lineage.root_id


def test_packet_bridge_derive_preserves_lineage():
    """PacketBridge derived packets preserve lineage chain."""
    try:
        from engine.packet.bridge import PacketBridge
    except ImportError:
        pytest.skip("engine.packet.bridge not implemented")
    bridge = PacketBridge()
    root = bridge.inflate_ingress(
        tenant_id="tenant-a",
        actor="engine",
        packet_type="graph_sync",
        payload={"entity_type": "Facility"},
    )
    derived = root.derive(packet_type="event", action="outcome_event", payload={"result": "ok"})
    assert derived.lineage.root_id == root.lineage.root_id
    assert derived.lineage.parent_id == root.header.packet_id
    assert derived.lineage.generation == 1
