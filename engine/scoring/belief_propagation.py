"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [scoring]
tags: [belief-propagation, bayesian, entropy, trust, scoring]
owner: engine-team
status: active
--- /L9_META ---

engine/scoring/belief_propagation.py

Bayesian belief propagation over trust signals.

Sequential Bayesian updating of a belief through an ordered chain of trust
signals, plus a status-to-trust mapping for GATE hop entries. Pure functions:
immutable inputs, no side effects, deterministic, outputs bounded to [0, 1].

Scope note:
    This module deliberately stops at chain propagation. Multi-signal
    *composite* scoring — fusing independent match dimensions into a single
    ranked score — is not implemented here, because CEG has not settled
    whether that fusion is Bayesian (each confirming signal raises belief) or
    weakest-link (the worst dimension caps the result). The two produce
    opposite orderings on mixed inputs. See DEFERRED.md.

Integration point:
    GATE: hop_trust_from_entry() -> propagate_chain() for hop-trace confidence.
"""

from __future__ import annotations

import math

# ── Trust tier constants ───────────────────────────────────────────────────────

TRUST_ENTAILMENT = 0.95  # Status: COMPLETED
TRUST_NEUTRAL = 0.60  # Status: PENDING, DELEGATED
TRUST_CONTRADICTION = 0.10  # Status: FAILED, TIMEOUT


# ── Bayesian update (core primitive) ──────────────────────────────────────────


def bayesian_update(prior: float, evidence: float) -> float:
    """
    Single-step Bayesian belief update.

    Formula:
        P(H|E) = P(E|H) * P(H) / P(E)

    Where:
        P(H)   = prior belief
        P(E|H) = evidence (trust signal)
        P(E)   = P(E|H)*P(H) + P(E|¬H)*P(¬H)

    Assumes a symmetric likelihood, P(E|¬H) = 1 - P(E|H). Under that
    assumption evidence = 0.5 is uninformative and leaves the belief
    unchanged; evidence below 0.5 lowers it, above 0.5 raises it.

    Args:
        prior:    Initial belief [0.0, 1.0]
        evidence: Trust signal   [0.0, 1.0]

    Returns:
        Posterior belief [0.0, 1.0]

    Examples:
        >>> round(bayesian_update(0.5, 0.9), 4)   # Neutral prior, strong evidence
        0.9
        >>> round(bayesian_update(0.8, 0.9), 4)   # Strong prior, strong evidence
        0.973
        >>> round(bayesian_update(0.2, 0.9), 4)   # Weak prior, strong evidence
        0.6923
    """
    if not (0.0 <= prior <= 1.0):
        msg = f"prior must be in [0.0, 1.0], got {prior}"
        raise ValueError(msg)
    if not (0.0 <= evidence <= 1.0):
        msg = f"evidence must be in [0.0, 1.0], got {evidence}"
        raise ValueError(msg)

    # P(E) = P(E|H)*P(H) + P(E|¬H)*P(¬H)
    p_e = evidence * prior + (1 - evidence) * (1 - prior)

    if p_e == 0.0:
        return 0.0

    # P(H|E) = P(E|H) * P(H) / P(E)
    posterior = (evidence * prior) / p_e
    return max(0.0, min(1.0, posterior))


# ── Entropy calculation ────────────────────────────────────────────────────────


def belief_entropy(p: float) -> float:
    """
    Shannon entropy of a binary belief state, in bits.

    H(p) = -p*log2(p) - (1-p)*log2(1-p)

    Where:
        p = 0.0 or 1.0 → H = 0.0 (complete certainty)
        p = 0.5        → H = 1.0 (maximum uncertainty)

    Args:
        p: Belief probability [0.0, 1.0]

    Returns:
        Entropy in bits [0.0, 1.0]

    Examples:
        >>> belief_entropy(0.5)
        1.0
        >>> round(belief_entropy(0.9), 4)
        0.469
        >>> belief_entropy(1.0)
        0.0
    """
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -p * math.log2(p) - (1 - p) * math.log2(1 - p)


# ── Chain propagation (GATE terminal confidence) ──────────────────────────────


def propagate_chain(trust_scores: list[float], prior: float = 0.5) -> float:
    """
    Sequential Bayesian belief propagation through an ordered chain.

    Use Case:
        GATE terminal confidence — "how confident is the chain, having passed
        through every hop?" Each hop's trust signal updates the running belief.

    Note that this accumulates evidence: a chain of confirming signals drives
    the belief up, and a single strong contradiction late in the chain pulls it
    back down. It is not a weakest-link measure.

    Args:
        trust_scores: Trust signals from the hop sequence, in order
        prior:        Initial belief

    Returns:
        Terminal belief [0.0, 1.0]

    Examples:
        >>> round(propagate_chain([0.9, 0.85, 0.8], prior=0.5), 4)
        0.9951
        >>> round(propagate_chain([0.9, 0.5, 0.2], prior=0.5), 4)
        0.6923
        >>> propagate_chain([], prior=0.7)
        0.7
    """
    if not trust_scores:
        return prior

    belief = prior
    for trust in trust_scores:
        belief = bayesian_update(belief, trust)
    return belief


# ── Hop trust derivation ──────────────────────────────────────────────────────


def hop_trust_from_entry(
    status: str,
    duration_ms: float,
    timeout_ms: float = 30000.0,
) -> float:
    """
    Derive a trust signal from a GATE HopEntry.

    Mapping:
        COMPLETED  → 0.95 (entailment tier), degraded by timeout proximity
        PENDING    → 0.60 (neutral tier)
        DELEGATED  → 0.60 (neutral tier)
        FAILED     → 0.10 (contradiction tier)
        TIMEOUT    → 0.10 (contradiction tier)
        unknown    → 0.60 (neutral tier)

    Timeout Penalty:
        If the hop completed but duration_ms / timeout_ms exceeds 0.5, trust
        degrades linearly from 0.95 down to 0.60 as the hop approaches its
        timeout — a hop that barely finished is weaker evidence than a fast one.

    Args:
        status:      HopEntry.status
        duration_ms: HopEntry.duration_ms
        timeout_ms:  HopEntry timeout limit (default 30s)

    Returns:
        Trust signal [0.0, 1.0]

    Examples:
        >>> hop_trust_from_entry("COMPLETED", 1000, 30000)
        0.95
        >>> round(hop_trust_from_entry("COMPLETED", 25000, 30000), 4)
        0.7167
        >>> hop_trust_from_entry("FAILED", 5000, 30000)
        0.1
    """
    if status == "COMPLETED":
        base_trust = TRUST_ENTAILMENT
    elif status in {"FAILED", "TIMEOUT"}:
        base_trust = TRUST_CONTRADICTION
    else:
        # PENDING, DELEGATED, and any unrecognized status
        base_trust = TRUST_NEUTRAL

    # Timeout proximity penalty (COMPLETED only)
    if status == "COMPLETED" and duration_ms > 0 and timeout_ms > 0:
        proximity = duration_ms / timeout_ms
        if proximity > 0.5:
            penalty_factor = (proximity - 0.5) / 0.5  # 0.0 → 1.0
            degraded = TRUST_ENTAILMENT - penalty_factor * (TRUST_ENTAILMENT - TRUST_NEUTRAL)
            return max(TRUST_NEUTRAL, degraded)

    return base_trust
