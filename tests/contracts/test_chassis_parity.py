"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [platform, chassis]
status: active
--- /L9_META ---

Contract test: the legacy chassis and the SDK chassis route the same action set.

engine.handlers.ACTION_HANDLERS is the single source of truth (CONTRACT-02).
These assertions are cheap tautologies today; they exist so that a future edit
which reintroduces a hand-maintained action list in either chassis fails here
rather than in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from constellation_node_sdk import clear_handlers, registered_actions

from chassis.handler_registration import register_engine_handlers
from engine.handlers import ACTION_HANDLERS

if TYPE_CHECKING:
    from collections.abc import Iterator

EXPECTED_ACTIONS = {
    "match",
    "sync",
    "admin",
    "outcomes",
    "resolve",
    "health",
    "healthcheck",
    "enrich",
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_handlers()
    yield
    clear_handlers()


def test_action_handlers_is_the_declared_action_set() -> None:
    assert set(ACTION_HANDLERS) == EXPECTED_ACTIONS


def test_sdk_registry_matches_action_handlers() -> None:
    register_engine_handlers()
    assert set(registered_actions()) == set(ACTION_HANDLERS)


def test_legacy_chassis_routes_action_handlers() -> None:
    """chassis/actions.py consumes ACTION_HANDLERS rather than rebuilding it."""
    from chassis import actions

    actions._init_engine()
    assert set(actions._engine_handlers) == set(ACTION_HANDLERS)


def test_entrypoint_dispatch_targets_both_chassis() -> None:
    from chassis.entrypoint import LEGACY, SDK, resolve_chassis

    assert (LEGACY, SDK) == ("legacy", "sdk")
    assert resolve_chassis() in (LEGACY, SDK)


def test_entrypoint_rejects_unknown_chassis(monkeypatch: pytest.MonkeyPatch) -> None:
    from chassis.entrypoint import resolve_chassis

    monkeypatch.setenv("L9_CHASSIS", "nope")
    with pytest.raises(ValueError, match="L9_CHASSIS"):
        resolve_chassis()


def test_entrypoint_rejects_legacy_chassis_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """W7-02: with the flag enabled, legacy chassis in prod fails startup."""
    from chassis.entrypoint import resolve_chassis
    from engine.config.settings import Settings

    monkeypatch.setenv("L9_CHASSIS", "legacy")
    prod = Settings(
        l9_env="prod",
        neo4j_password="test-pw",
        api_secret_key="test-key",
        require_sdk_chassis_in_prod=True,
    )
    with patch("engine.config.settings.settings", prod):
        with pytest.raises(ValueError, match="production"):
            resolve_chassis()


def test_entrypoint_allows_sdk_chassis_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """W7-02: sdk chassis in prod is accepted with the flag enabled."""
    from chassis.entrypoint import resolve_chassis
    from engine.config.settings import Settings

    monkeypatch.setenv("L9_CHASSIS", "sdk")
    prod = Settings(
        l9_env="prod",
        neo4j_password="test-pw",
        api_secret_key="test-key",
        require_sdk_chassis_in_prod=True,
    )
    with patch("engine.config.settings.settings", prod):
        assert resolve_chassis() == "sdk"
