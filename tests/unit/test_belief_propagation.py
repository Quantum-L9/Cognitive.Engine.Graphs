"""
Unit tests — Belief Propagation.

Coverage:
  - Bayesian update correctness
  - Binary Shannon entropy
  - Chain propagation
  - Hop trust derivation
  - Edge cases and bounds
"""

from __future__ import annotations

import pytest

from engine.scoring.belief_propagation import (
    TRUST_CONTRADICTION,
    TRUST_ENTAILMENT,
    TRUST_NEUTRAL,
    bayesian_update,
    belief_entropy,
    hop_trust_from_entry,
    propagate_chain,
)


class TestBayesianUpdate:
    """Test Bayesian belief update primitive."""

    def test_neutral_prior_strong_evidence(self):
        result = bayesian_update(0.5, 0.9)
        assert 0.89 < result < 0.91

    def test_strong_prior_strong_evidence(self):
        result = bayesian_update(0.8, 0.9)
        assert 0.97 < result < 0.98

    def test_weak_prior_strong_evidence(self):
        result = bayesian_update(0.2, 0.9)
        assert 0.68 < result < 0.70

    def test_neutral_prior_weak_evidence(self):
        result = bayesian_update(0.5, 0.2)
        assert 0.19 < result < 0.21

    def test_neutral_evidence_leaves_belief_unchanged(self):
        for prior in (0.1, 0.4, 0.75, 0.9):
            assert bayesian_update(prior, 0.5) == pytest.approx(prior)

    def test_extremes(self):
        assert bayesian_update(1.0, 1.0) == 1.0
        assert bayesian_update(0.0, 0.0) == 0.0
        assert bayesian_update(0.0, 1.0) == 0.0

    def test_output_bounded(self):
        for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
            for e in [0.1, 0.3, 0.5, 0.7, 0.9]:
                result = bayesian_update(p, e)
                assert 0.0 <= result <= 1.0

    def test_bounds_validation_prior(self):
        with pytest.raises(ValueError, match="prior"):
            bayesian_update(-0.1, 0.5)

    def test_bounds_validation_evidence(self):
        with pytest.raises(ValueError, match="evidence"):
            bayesian_update(0.5, 1.1)


class TestBeliefEntropy:
    """Test binary Shannon entropy."""

    def test_maximum_at_half(self):
        assert belief_entropy(0.5) == pytest.approx(1.0)

    def test_zero_at_certainty(self):
        assert belief_entropy(0.0) == 0.0
        assert belief_entropy(1.0) == 0.0

    def test_symmetry(self):
        for p in (0.1, 0.25, 0.4):
            assert belief_entropy(p) == pytest.approx(belief_entropy(1 - p))

    def test_known_value(self):
        assert belief_entropy(0.9) == pytest.approx(0.469, abs=0.001)

    def test_output_bounded(self):
        for p in (0.0, 0.05, 0.3, 0.5, 0.8, 1.0):
            assert 0.0 <= belief_entropy(p) <= 1.0


class TestPropagateChain:
    """Test chain terminal confidence."""

    def test_strong_accumulation(self):
        result = propagate_chain([0.9, 0.85, 0.8], prior=0.5)
        assert result > 0.98

    def test_contradicting_terminal_hop_pulls_belief_down(self):
        # 0.5 is uninformative; the final 0.2 signal drags an accumulated
        # 0.9 belief back toward the midpoint but does not invert it.
        result = propagate_chain([0.9, 0.5, 0.2], prior=0.5)
        assert 0.68 < result < 0.70

    def test_repeated_contradiction_collapses_belief(self):
        result = propagate_chain([0.2, 0.2, 0.2], prior=0.5)
        assert result < 0.05

    def test_empty_chain_returns_prior(self):
        assert propagate_chain([], prior=0.7) == 0.7

    def test_single_hop(self):
        result = propagate_chain([0.9], prior=0.5)
        assert 0.89 < result < 0.91

    def test_order_independent(self):
        # Sequential Bayesian updating with symmetric likelihoods commutes.
        forward = propagate_chain([0.9, 0.3, 0.7], prior=0.4)
        reverse = propagate_chain([0.7, 0.3, 0.9], prior=0.4)
        assert forward == pytest.approx(reverse)

    def test_output_bounded(self):
        for chain in ([0.1, 0.9], [0.99] * 10, [0.01] * 10, [0.5, 0.5]):
            assert 0.0 <= propagate_chain(chain) <= 1.0


class TestHopTrustFromEntry:
    """Test GATE HopEntry trust derivation."""

    def test_completed_fast(self):
        trust = hop_trust_from_entry("COMPLETED", 1000, 30000)
        assert trust == TRUST_ENTAILMENT

    def test_completed_near_timeout(self):
        trust = hop_trust_from_entry("COMPLETED", 25000, 30000)
        assert TRUST_NEUTRAL < trust < TRUST_ENTAILMENT

    def test_completed_at_timeout(self):
        trust = hop_trust_from_entry("COMPLETED", 30000, 30000)
        assert trust == pytest.approx(TRUST_NEUTRAL, abs=0.01)

    def test_timeout_penalty_is_monotonic(self):
        previous = TRUST_ENTAILMENT + 1.0
        for duration in (1000, 15000, 20000, 25000, 30000):
            trust = hop_trust_from_entry("COMPLETED", duration, 30000)
            assert trust <= previous
            previous = trust

    def test_pending_status(self):
        assert hop_trust_from_entry("PENDING", 5000, 30000) == TRUST_NEUTRAL

    def test_delegated_status(self):
        assert hop_trust_from_entry("DELEGATED", 5000, 30000) == TRUST_NEUTRAL

    def test_failed_status(self):
        assert hop_trust_from_entry("FAILED", 5000, 30000) == TRUST_CONTRADICTION

    def test_timeout_status(self):
        assert hop_trust_from_entry("TIMEOUT", 30000, 30000) == TRUST_CONTRADICTION

    def test_unknown_status(self):
        assert hop_trust_from_entry("UNKNOWN_XYZ", 5000, 30000) == TRUST_NEUTRAL

    def test_output_bounded(self):
        for status in ("COMPLETED", "PENDING", "FAILED", "TIMEOUT"):
            trust = hop_trust_from_entry(status, 10000, 30000)
            assert 0.0 <= trust <= 1.0
