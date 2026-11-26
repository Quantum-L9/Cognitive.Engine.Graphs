"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, scoring, belief-propagation]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_hop_trust_neutral.py
SCR-05: a GATE hop that carries no information (PENDING, DELEGATED, unknown)
maps to the trust ``bayesian_update`` treats as uninformative, so it leaves the
chain's belief where it was. The module is dormant (no engine caller).
"""

from __future__ import annotations

import pytest

from engine.config.settings import Settings, settings
from engine.scoring.belief_propagation import (
    TRUST_ENTAILMENT,
    TRUST_NEUTRAL,
    TRUST_NEUTRAL_LEGACY,
    bayesian_update,
    hop_trust_from_entry,
    propagate_chain,
)

pytestmark = pytest.mark.unit

NEUTRAL_STATUSES = ("PENDING", "DELEGATED", "SOMETHING_ELSE")


def test_the_shipped_default_is_on() -> None:
    assert Settings().hop_trust_neutral_uninformative_enabled is True


@pytest.mark.parametrize("status", NEUTRAL_STATUSES)
def test_a_neutral_hop_leaves_any_belief_unchanged(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hop_trust_neutral_uninformative_enabled", True)
    trust = hop_trust_from_entry(status, 5000, 30000)
    assert trust == TRUST_NEUTRAL == 0.5
    for prior in (0.1, 0.5, 0.73, 0.9):
        assert bayesian_update(prior, trust) == pytest.approx(prior)
    # A chain of uninformative hops is no evidence at all.
    assert propagate_chain([trust] * 5, prior=0.3) == pytest.approx(0.3)


def test_a_completed_hop_at_its_timeout_is_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hop_trust_neutral_uninformative_enabled", True)
    assert hop_trust_from_entry("COMPLETED", 30000, 30000) == pytest.approx(0.5)
    # 25 s of 30: two thirds of the way from 0.95 down to 0.5.
    assert hop_trust_from_entry("COMPLETED", 25000, 30000) == pytest.approx(0.65)
    assert hop_trust_from_entry("COMPLETED", 1000, 30000) == TRUST_ENTAILMENT


@pytest.mark.parametrize("status", NEUTRAL_STATUSES)
def test_flag_off_restores_the_legacy_neutral_that_raised_belief(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hop_trust_neutral_uninformative_enabled", False)
    trust = hop_trust_from_entry(status, 5000, 30000)
    assert trust == TRUST_NEUTRAL_LEGACY == 0.60
    # The defect: a hop with no information raised the belief.
    assert bayesian_update(0.5, trust) == pytest.approx(0.6)
    assert hop_trust_from_entry("COMPLETED", 30000, 30000) == pytest.approx(0.60)
