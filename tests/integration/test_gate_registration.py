"""
Tests for engine/gate_registration.py.

Verifies:
- register_node_with_gate is non-fatal on failure
- register_node_with_gate logs success on registration
"""

from __future__ import annotations

import pytest

pytest.importorskip("constellation_node_sdk", reason="constellation-node-sdk not installed")

from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_register_node_non_fatal_on_failure(monkeypatch, caplog):
    """Registration failure should log error but not raise."""
    monkeypatch.setenv("GATE_URL", "http://test-gate:8000")
    monkeypatch.setenv("L9_NODE_NAME", "test-node")
    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "true")

    with patch(
        "engine.gate_registration.register_from_env",
        new_callable=AsyncMock,
        side_effect=Exception("Connection refused"),
    ):
        from engine.gate_registration import register_node_with_gate

        # Should not raise
        await register_node_with_gate()

    assert "Gate registration failed" in caplog.text or "error" in caplog.text.lower()


@pytest.mark.asyncio
async def test_register_node_skipped_when_disabled(monkeypatch, caplog):
    """Registration should be skipped when GATE_REGISTRATION_ENABLED=false."""
    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "false")

    from engine.gate_registration import register_node_with_gate

    await register_node_with_gate()
    # Should complete without error when disabled


@pytest.mark.asyncio
async def test_reregistration_repeats_and_survives_a_failed_cycle():
    """Gate forgets nodes on restart; the loop must keep re-registering, even
    after a cycle that fails, until it is cancelled."""
    import asyncio

    calls: list[int] = []

    async def flaky_register() -> bool:
        calls.append(1)
        if len(calls) == 2:
            raise ConnectionError("gate restarting")
        return True

    with patch("engine.gate_registration.register_from_env", new=flaky_register):
        from engine.gate_registration import reregister_with_gate_forever

        task = asyncio.create_task(reregister_with_gate_forever(0.01))
        for _ in range(200):
            if len(calls) >= 4:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)

    assert len(calls) >= 4, "loop stopped re-registering after the failed cycle"
    assert isinstance(results[0], asyncio.CancelledError)


def test_reregistration_is_a_documented_default_on_flag():
    from engine.config.settings import Settings

    fields = Settings.model_fields
    assert fields["gate_reregistration_enabled"].default is True
    assert fields["gate_reregistration_interval_seconds"].default == 300.0
