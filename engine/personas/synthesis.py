"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [personas]
tags: [synthesis, primitives, tier-3]
owner: engine-team
status: active
--- /L9_META ---

Primitive-aware synthesis for multi-persona output weighting.

Scores each persona output by alignment of its trait vector fingerprint with
the query's needed primitives (verification, comparison, generation, quantification).
Normalizes to weights (minimum 10% per persona to avoid total exclusion).
Builds a weighted synthesis prompt for the LLM.

Reference: "Algorithmic Primitives and Compositional Geometry of Reasoning
in Language Models" (arXiv 2510.15987v1)
"""

from __future__ import annotations

import logging
import math

from engine.personas.constants import persona_settings
from engine.personas.types import FeatureVector, PersonaOutput, WeightedOutput

logger = logging.getLogger(__name__)


def _compute_primitive_alignment(persona_output: PersonaOutput, feature_vector: FeatureVector) -> float:
    """
    Compute alignment between a persona's trait vector and the query's
    primitive needs (verification, comparison, generation, quantitative).

    Returns a raw alignment score (not normalized).
    """
    fv = feature_vector.to_dict()
    tv = persona_output.trait_vector.to_dict()

    alignment = 0.0
    for dim in persona_settings.primitive_dimensions:
        alignment += fv.get(dim, 0.0) * tv.get(dim, 0.0)

    return alignment


def compute_synthesis_weights(
    persona_outputs: list[PersonaOutput],
    feature_vector: FeatureVector,
) -> list[WeightedOutput]:
    """
    Compute primitive-aware synthesis weights for each persona output.

    Each weight reflects how well the persona's trait fingerprint aligns with
    the query's needed primitives. Weights are normalized to sum to 1.0, with
    a minimum floor (min_persona_weight) to avoid total exclusion.

    Args:
        persona_outputs: List of outputs from executed personas.
        feature_vector: The query's classified feature vector.

    Returns:
        List of WeightedOutput dicts with weights summing to ~1.0.
    """
    if not persona_outputs:
        return []

    if len(persona_outputs) == 1:
        return [{"output": persona_outputs[0], "weight": 1.0}]

    # Compute raw alignment scores
    raw_scores: list[float] = []
    for output in persona_outputs:
        raw_scores.append(_compute_primitive_alignment(output, feature_vector))

    # Normalize: convert to proportions, apply minimum floor
    total_raw = sum(raw_scores)
    min_w = persona_settings.min_persona_weight
    n = len(persona_outputs)

    if math.isclose(total_raw, 0.0, abs_tol=1e-9):
        # Equal weights when no alignment signal
        equal_w = 1.0 / n
        return [{"output": output, "weight": equal_w} for output in persona_outputs]

    weights = _floored_proportions(raw_scores, min_w)

    result: list[WeightedOutput] = []
    for output, weight in zip(persona_outputs, weights, strict=True):
        result.append({"output": output, "weight": weight})

    return result


def _floored_proportions(raw_scores: list[float], min_w: float) -> list[float]:
    """Proportional weights summing to 1 with every weight >= ``min_w``.

    Flooring and then renormalising pushed floored entries back below the
    floor ([1, 0, 0] -> [0.833, 0.083, 0.083], CEG-COR-115). Instead, entries
    that fall below the floor are fixed at ``min_w`` and the remaining
    ``1 - k * min_w`` is distributed proportionally among the rest, repeating
    until no free entry falls below the floor. When ``n * min_w > 1`` the floor
    is infeasible and the weights are equal. Weights stay monotone in the raw
    alignment scores.
    """
    n = len(raw_scores)
    if n * min_w > 1.0:
        return [1.0 / n] * n

    fixed: set[int] = set()
    weights = [0.0] * n
    while True:
        free = [i for i in range(n) if i not in fixed]
        remaining = 1.0 - len(fixed) * min_w
        free_total = sum(raw_scores[i] for i in free)
        for i in fixed:
            weights[i] = min_w
        for i in free:
            if math.isclose(free_total, 0.0, abs_tol=1e-9):
                weights[i] = remaining / len(free)
            else:
                weights[i] = remaining * raw_scores[i] / free_total
        below = {i for i in free if weights[i] < min_w}
        if not below:
            return weights
        fixed |= below


def build_synthesis_prompt(
    persona_outputs: list[PersonaOutput],
    feature_vector: FeatureVector,
    original_query: str = "",
) -> str:
    """
    Build a weighted synthesis prompt that tells the LLM how to combine
    persona outputs based on their primitive relevance.

    The prompt includes:
    - Weight guidance: persona name + relevance percentage
    - Each persona's output content
    - Instructions to prioritize higher-relevance perspectives
    - Instructions to remove persona identity traces

    Args:
        persona_outputs: Outputs from all executed personas.
        feature_vector: The query's classified feature vector.
        original_query: The original user query (for context).

    Returns:
        Synthesis prompt string ready for LLM submission.
    """
    if not persona_outputs:
        return ""

    weighted = compute_synthesis_weights(persona_outputs, feature_vector)

    # Build weight guidance header
    weight_lines: list[str] = []
    for entry in weighted:
        pct = round(entry["weight"] * 100, 1)
        weight_lines.append(f"  [{entry['output'].persona_name}] (relevance: {pct}%)")

    contributions: list[str] = []
    for entry in weighted:
        pct = round(entry["weight"] * 100, 1)
        contributions.append(f"--- [{entry['output'].persona_name}] (relevance: {pct}%) ---\n{entry['output'].content}")

    prompt = (
        "You are synthesizing multiple cognitive perspectives into a single coherent response.\n\n"
        "Weight contributions based on relevance scores:\n" + "\n".join(weight_lines) + "\n\n"
        "Prioritize insights from higher-relevance perspectives. "
        "Remove persona identity traces from the final output.\n\n"
    )

    if original_query:
        prompt += f"Original query: {original_query}\n\n"

    prompt += "Persona contributions:\n\n" + "\n\n".join(contributions)

    prompt += (
        "\n\nSynthesize the above perspectives into a single, coherent, well-structured response. "
        "Give greater weight to higher-relevance contributions. "
        "Do not reference individual personas by name in your output."
    )

    return prompt
