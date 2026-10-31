"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, boot, lifecycle, asyncio, cancellation]
owner: engine-team
status: active
--- /L9_META ---

GraphLifecycle cancellation handling (SonarCloud python:S7497).

The compliance flush loop ends as a cancelled task instead of completing
normally, and shutdown() absorbs only the cancellation of the task it cancelled:
a cancellation of shutdown() itself still reaches its caller.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from engine.boot import GraphLifecycle
from engine.config.settings import settings

pytestmark = pytest.mark.unit


async def test_cancelled_flush_loop_ends_as_a_cancelled_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compliance_flush_interval", 3600)
    task = asyncio.create_task(GraphLifecycle()._compliance_flush_loop())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


async def test_shutdown_stops_the_flush_task_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compliance_flush_interval", 3600)
    lifecycle = GraphLifecycle()
    flush_task = asyncio.create_task(lifecycle._compliance_flush_loop())
    lifecycle._compliance_flush_task = flush_task
    await asyncio.sleep(0)
    with patch("engine.boot.get_state") as get_state:
        get_state.return_value.compliance_engines = {}
        get_state.return_value.shutdown = AsyncMock()
        await lifecycle.shutdown()
    assert flush_task.cancelled()
    assert lifecycle._compliance_flush_task is None


async def test_cancelling_shutdown_itself_propagates() -> None:
    """A cancellation of shutdown() while it waits on the flush task is not swallowed."""
    lifecycle = GraphLifecycle()

    async def slow_cleanup_on_cancel() -> None:
        # Cleans up briefly when cancelled, then ends cancelled: shutdown() is
        # still waiting on it when shutdown() itself is cancelled.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    lifecycle._compliance_flush_task = asyncio.create_task(slow_cleanup_on_cancel())
    await asyncio.sleep(0)
    with patch("engine.boot.get_state") as get_state:
        get_state.return_value.compliance_engines = {}
        get_state.return_value.shutdown = AsyncMock()
        shutdown = asyncio.create_task(lifecycle.shutdown())
        await asyncio.sleep(0.01)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, timeout=5)
        get_state.return_value.shutdown.assert_not_awaited()


# ── BOOT_CANCELLATION_PROPAGATES=false restores the previous behavior ─────────


async def test_flag_off_flush_loop_returns_normally_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compliance_flush_interval", 3600)
    monkeypatch.setattr(settings, "boot_cancellation_propagates", False)
    task = asyncio.create_task(GraphLifecycle()._compliance_flush_loop())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not task.cancelled()
    assert task.exception() is None


async def test_flag_off_shutdown_swallows_its_own_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "boot_cancellation_propagates", False)
    lifecycle = GraphLifecycle()

    async def slow_cleanup_on_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    lifecycle._compliance_flush_task = asyncio.create_task(slow_cleanup_on_cancel())
    await asyncio.sleep(0)
    with patch("engine.boot.get_state") as get_state:
        get_state.return_value.compliance_engines = {}
        get_state.return_value.shutdown = AsyncMock()
        shutdown = asyncio.create_task(lifecycle.shutdown())
        await asyncio.sleep(0.01)
        shutdown.cancel()
        await asyncio.wait_for(shutdown, timeout=5)
        get_state.return_value.shutdown.assert_awaited_once()
