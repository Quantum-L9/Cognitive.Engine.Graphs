"""Integration — DimensionWeight writer/reader round trip (RU-022 / CEG-COR-030, RG-029).

``SignalWeightCalculator._store_weights`` MERGEs ``(:DimensionWeight
{dimension, tenant})``; ``ScoringAssembler.load_learned_weights`` used to read
``dw.dimension_name`` / ``dw.domain_id``, which the writer never sets, and so
always returned ``{}``. The reader now filters on the writer's keys.

Runs against the per-domain ``orbital-logistics`` database of the synthetic pack
(ADR-112 Stage 4 / A16; handlers route by ``database=domain_spec.domain.id``),
which needs the enterprise testcontainer. The pack leaves its feedback loop off,
and the reader returns ``{}`` while it is off, so both tests run on a copy with
the loop and signal weights on — the legacy reader then finds nothing because
of its keys, not because the loop is disabled.
"""

from __future__ import annotations

import pytest

from tests.fixtures.orbital_match import with_feedback_loop


@pytest.fixture
def learning_spec(domain_spec):
    return with_feedback_loop(domain_spec)


async def _clear(graph_driver) -> None:
    await graph_driver.execute_query(
        "MATCH (dw:DimensionWeight {tenant: $tenant}) DETACH DELETE dw",
        parameters={"tenant": "orbital-logistics"},
        database="orbital-logistics",
    )


async def _store(graph_driver, domain_spec, weights: dict[str, float]) -> None:
    from engine.feedback.signal_weights import SignalWeightCalculator

    calc = SignalWeightCalculator(graph_driver, domain_spec)
    await calc._store_weights({name: {"weight": w, "confidence": 0.9, "ci_width": 0.1} for name, w in weights.items()})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reader_returns_weights_the_writer_stored(graph_driver, learning_spec, domain_database):
    from engine.scoring.assembler import ScoringAssembler

    await _clear(graph_driver)
    try:
        await _store(graph_driver, learning_spec, {"depot_reliability": 0.2, "port_capacity": 1.4})
        weights = await ScoringAssembler(learning_spec, graph_driver=graph_driver).load_learned_weights()
    finally:
        await _clear(graph_driver)

    assert weights == {"depot_reliability": pytest.approx(0.2), "port_capacity": pytest.approx(1.4)}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_reader_finds_nothing(graph_driver, learning_spec, domain_database, monkeypatch):
    """C-21 rollback reproduces the defect: the legacy reader's keys are absent on written nodes."""
    from engine.config.settings import settings
    from engine.scoring.assembler import ScoringAssembler

    monkeypatch.setattr(settings, "learned_weights_tenant_keyed", False)
    await _clear(graph_driver)
    try:
        await _store(graph_driver, learning_spec, {"depot_reliability": 0.2})
        weights = await ScoringAssembler(learning_spec, graph_driver=graph_driver).load_learned_weights()
    finally:
        await _clear(graph_driver)

    assert weights == {}
