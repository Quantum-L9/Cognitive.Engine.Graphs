"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, causal, counterfactual, outcomes]
owner: engine-team
status: active
--- /L9_META ---

CEG-NATIVE-CLOSURE-001 N7b (F10, C14): a keyed outcome replay reruns the
counterfactual stage, so a transient failure on the first write is recovered.
The rerun is safe only while scenario writes MERGE on their semantic key
(``counterfactual_dedupe_and_spec_label``); with that flag off a replay keeps
skipping the stage. Graph effects are proven in
tests/integration/test_counterfactual_closure.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.causal.counterfactual import CounterfactualGenerator
from engine.config.settings import settings
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = [pytest.mark.unit, pytest.mark.finding("F10")]

_TENANT = "orbital-logistics"


def _spec() -> Any:
    spec = synthetic_loader().load_domain(_TENANT)
    return spec.model_copy(update={"counterfactual": spec.counterfactual.model_copy(update={"enabled": True})})


class _Driver:
    """Answers the keyed outcome MERGE as a replay (``created`` false) or a first write."""

    def __init__(self, *, replay: bool) -> None:
        self._replay = replay

    async def execute_query(self, cypher: str, parameters: dict[str, Any] | None = None, **_: Any) -> list[Any]:
        if "MERGE (o:" not in cypher:
            return []
        if not self._replay:
            return [{"outcome_id": "replay-1", "created": True}]
        return [
            {
                "outcome_id": "replay-1",
                "created": False,
                "stored_match_id": "m-1",
                "stored_candidate_id": "c-1",
                "stored_outcome": "failure",
                "stored_tenant": _TENANT,
            }
        ]


def _wire(driver: _Driver) -> None:
    from engine.handlers import init_dependencies
    from engine.state import get_state

    loader = MagicMock()
    loader.load_domain.return_value = _spec()
    loader.list_domains.return_value = [_TENANT]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    get_state()._tenant_allowlist = {_TENANT}


def _payload() -> dict[str, Any]:
    return {"match_id": "m-1", "candidate_id": "c-1", "outcome": "failure", "outcome_id": "replay-1"}


@pytest.fixture
def generate(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(CounterfactualGenerator, "generate_for_outcome", mock)
    return mock


async def test_replay_reruns_the_counterfactual_stage(generate: AsyncMock) -> None:
    from engine.handlers import handle_outcomes

    _wire(_Driver(replay=True))
    result = await handle_outcomes(_TENANT, _payload())

    assert result == {"status": "recorded", "outcome_id": "replay-1", "replayed": True}
    generate.assert_awaited_once_with("replay-1")


async def test_first_write_runs_the_stage_once(generate: AsyncMock) -> None:
    from engine.handlers import handle_outcomes

    _wire(_Driver(replay=False))
    result = await handle_outcomes(_TENANT, _payload())

    assert "replayed" not in result
    generate.assert_awaited_once_with("replay-1")


async def test_dedupe_flag_off_keeps_skipping_on_replay(generate: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the semantic-key MERGE a rerun would duplicate scenarios, so the replay skips it."""
    from engine.handlers import handle_outcomes

    monkeypatch.setattr(settings, "counterfactual_dedupe_and_spec_label", False)
    _wire(_Driver(replay=True))
    result = await handle_outcomes(_TENANT, _payload())

    assert result["replayed"] is True
    generate.assert_not_awaited()


async def test_replay_stage_failure_is_logged_not_raised(generate: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    from engine.handlers import handle_outcomes

    generate.side_effect = RuntimeError("still failing")
    _wire(_Driver(replay=True))
    result = await handle_outcomes(_TENANT, _payload())

    assert result == {"status": "recorded", "outcome_id": "replay-1", "replayed": True}
    assert any("Counterfactual generation failed for replay-1" in r.getMessage() for r in caplog.records)
