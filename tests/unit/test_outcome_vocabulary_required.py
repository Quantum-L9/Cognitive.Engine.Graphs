"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-neutrality, outcomes]
owner: engine-team
status: active
--- /L9_META ---

Stage 2 follow-up, item 5 / M2 ("require it, packs declare").

``feedbackloop.outcome_node_label`` has no engine default. A pack that writes or
reads outcomes declares it; on a pack that declares none every outcome write
and read fails with a named error before any Cypher is sent.
``outcome_vocabulary_required_enabled`` off restores the former default.

Proved on the synthetic packs: orbital-logistics declares ``DockingResult``,
cheese-aging declares nothing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.outcome_vocabulary import OutcomeNodeLabelUndeclaredError
from engine.config.schema import DomainSpec, FeedbackLoopSpec
from engine.config.settings import settings
from tests.fixtures.domain_contracts import synthetic_loader

pytestmark = pytest.mark.unit

_OUTCOME = {"match_id": "m1", "candidate_id": "c-1", "outcome": "success"}


def _bind(spec: DomainSpec) -> list[str]:
    from engine.handlers import init_dependencies
    from engine.state import get_state

    sent: list[str] = []

    async def _record(*, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        sent.append(cypher)
        return [{"outcome_id": "out_x"}]

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=_record)
    loader = MagicMock()
    loader.load_domain.return_value = spec
    get_state().reset()
    init_dependencies(driver, loader)
    return sent


@pytest.fixture
def _reset_state():
    from engine.state import _reset_singleton

    yield
    _reset_singleton()


def test_schema_borrows_no_outcome_label() -> None:
    assert FeedbackLoopSpec().outcome_node_label is None
    assert synthetic_loader().load_domain("cheese-aging").feedbackloop.outcome_node_label is None


def test_every_served_pack_declares_its_outcome_label() -> None:
    """Freight and mortgage declare the label their graphs already hold, so nothing migrates."""
    loader = DomainPackLoader(config_path="domains")
    labels = {domain: loader.load_domain(domain).feedbackloop.outcome_node_label for domain in loader.list_domains()}
    assert labels == {
        "freight-matching": "TransactionOutcome",
        "mortgage-brokerage": "TransactionOutcome",
        "plasticos": "TransactionOutcome",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_state")
async def test_outcome_write_on_an_undeclared_pack_is_refused_before_any_cypher() -> None:
    from engine.handlers import OutcomeVocabularyUndeclaredError, ValidationError, handle_outcomes

    sent = _bind(synthetic_loader().load_domain("cheese-aging"))
    with pytest.raises(OutcomeVocabularyUndeclaredError, match="'cheese-aging' declares no feedbackloop") as info:
        await handle_outcomes("cheese-aging", dict(_OUTCOME))
    assert isinstance(info.value, ValidationError)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_state")
async def test_outcome_write_uses_the_declared_label() -> None:
    from engine.handlers import handle_outcomes

    sent = _bind(synthetic_loader().load_domain("orbital-logistics"))
    result = await handle_outcomes("orbital-logistics", dict(_OUTCOME))
    assert result["status"] == "recorded"
    assert "CREATE (o:DockingResult {" in sent[0]
    assert "TransactionOutcome" not in "\n".join(sent)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_state")
async def test_flag_off_restores_the_former_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.handlers import handle_outcomes

    monkeypatch.setattr(settings, "outcome_vocabulary_required_enabled", False)
    sent = _bind(synthetic_loader().load_domain("cheese-aging"))
    result = await handle_outcomes("cheese-aging", dict(_OUTCOME))
    assert result["status"] == "recorded"
    assert "CREATE (o:TransactionOutcome {" in sent[0]


@pytest.mark.asyncio
async def test_sync_outcome_items_fail_explicitly_instead_of_being_skipped() -> None:
    """The sync-outcome path shares the writer; an undeclared label is not a malformed item to skip."""
    from engine.handlers import OutcomeVocabularyUndeclaredError, _sync_outcomes_unified

    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    spec = synthetic_loader().load_domain("cheese-aging")
    with pytest.raises(OutcomeVocabularyUndeclaredError):
        await _sync_outcomes_unified(driver, spec, "cheese-aging", [{"match_id": "m1", "outcome": "positive"}])
    driver.execute_query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_state")
async def test_outcome_read_on_an_undeclared_pack_is_refused() -> None:
    from engine.handlers import OutcomeVocabularyUndeclaredError, handle_admin

    sent = _bind(synthetic_loader().load_domain("cheese-aging"))
    with pytest.raises(OutcomeVocabularyUndeclaredError, match="generate_calibration_pairs"):
        await handle_admin("cheese-aging", {"subaction": "generate_calibration_pairs", "domain_id": "cheese-aging"})
    assert sent == []


@pytest.mark.asyncio
async def test_feedback_modules_refuse_an_undeclared_pack() -> None:
    from engine.feedback.signal_weights import SignalWeightCalculator

    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    calculator = SignalWeightCalculator(driver, synthetic_loader().load_domain("cheese-aging"))
    with pytest.raises(OutcomeNodeLabelUndeclaredError, match="signal weight recalculation"):
        await calculator.recalculate_weights()
    driver.execute_query.assert_not_called()


def _attention_dimension() -> MagicMock:
    # ScoringDimensionSpec declares no `metadata` field, so the dimension is
    # duck-typed here exactly as the assembler reads it (cf. test_hgkr_pass2).
    dim = MagicMock()
    dim.name = "tasting_history"
    dim.candidateprop = "humidity_pct"
    dim.defaultwhennull = 0.0
    dim.metadata = {"success_value": "tasted"}
    return dim


def test_preference_attention_reads_the_declared_outcome_label() -> None:
    from engine.scoring.assembler import ScoringAssembler

    with pytest.raises(OutcomeNodeLabelUndeclaredError, match="preference_attention dimension 'tasting_history'"):
        ScoringAssembler(synthetic_loader().load_domain("cheese-aging"))._compile_preference_attention(
            _attention_dimension()
        )
    expression = ScoringAssembler(synthetic_loader().load_domain("orbital-logistics"))._compile_preference_attention(
        _attention_dimension()
    )
    assert ":DockingResult)" in expression
    assert "TransactionOutcome" not in expression
