"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, handlers, match, arbitration, ordering]
owner: engine-team
status: active
--- /L9_META ---

ND-ARB / ND-TIE (ceg-scoring-audit L3, D4) against a real Neo4j.

State is written by the engine's own write path (``handle_sync`` on the
orbital-depots endpoint, through the real pack loader) and read by
``handle_match``. The synthetic
orbital-logistics pack scores ``0.6 * reliability + 0.3 * log(1 + ports) /
log(1001)``; ``decision_arbitration`` is overlaid with ``model_copy`` (the
tests/fixtures/orbital_match.py pattern), since the pack declares none.

Expected scores (computed from the pack's expression):

=========  ===========  =====  ==============  =====
depot      reliability  ports  port_capacity   score
=========  ===========  =====  ==============  =====
wide       0.90         900    0.9847          0.835
mid        0.50         500    0.8998          0.570
narrow     0.95         20     0.4407          0.702
risky      0.55         900    0.9847          0.625
safe       0.90         2      0.1590          0.588
=========  ===========  =====  ==============  =====
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.config.schema import ArbitrationConstraintSpec, DecisionArbitrationSpec
from tests.fixtures.orbital_match import DIRECTION, ORBITAL, serve

pytestmark = pytest.mark.integration


def _depot(depot_id: str, reliability: float, ports: int) -> dict[str, Any]:
    return {"depot_id": depot_id, "reliability": reliability, "docking_ports": ports, "accepts_hazmat": True}


def _arbitrated(spec: Any, *constraints: ArbitrationConstraintSpec) -> Any:
    arbitration = DecisionArbitrationSpec(enabled=True, constraints=list(constraints))
    return spec.model_copy(update={"decision_arbitration": arbitration})


async def _sync(depots: list[dict[str, Any]]) -> None:
    from engine.handlers import handle_sync

    result = await handle_sync(ORBITAL, {"entity_type": "orbital-depots", "batch": depots})
    assert result.get("status") in ("ok", "success"), result


async def _match(top_n: int) -> list[dict[str, Any]]:
    from engine.handlers import handle_match

    result = await handle_match(ORBITAL, {"query": {}, "match_direction": DIRECTION, "top_n": top_n})
    return list(result["candidates"])


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["candidate"]["depot_id"] for row in rows]


@pytest.mark.asyncio
async def test_rank_is_monotone_in_returned_score_under_soft_penalty(engine_deps, graph_driver, domain_spec, clean_db):
    soft = ArbitrationConstraintSpec(dimension="port_capacity", threshold=0.6, hard=False, penalty=0.25)
    await _sync([_depot("wide", 0.90, 900), _depot("mid", 0.50, 500), _depot("narrow", 0.95, 20)])
    serve(graph_driver, _arbitrated(domain_spec, soft))

    rows = await _match(top_n=3)
    scores = [row["score"] for row in rows]

    # narrow (0.702) is penalised to 0.1755 and ranks after mid (0.570): the
    # legacy post-cut pass returned it second with the lower score.
    assert _ids(rows) == ["wide", "mid", "narrow"]
    assert scores == sorted(scores, reverse=True)
    assert scores[2] == pytest.approx(0.702 * 0.25, abs=1e-3)
    assert [row["rank"] for row in rows] == [1, 2, 3]


@pytest.mark.asyncio
async def test_top_one_is_admissible_when_the_best_score_is_hard_rejected(
    engine_deps, graph_driver, domain_spec, clean_db
):
    hard = ArbitrationConstraintSpec(dimension="depot_reliability", threshold=0.6, hard=True)
    await _sync([_depot("risky", 0.55, 900), _depot("safe", 0.90, 2)])
    serve(graph_driver, _arbitrated(domain_spec, hard))

    rows = await _match(top_n=1)

    # risky outscores safe (0.625 > 0.588) but fails the hard constraint; the
    # legacy post-cut pass cut to [risky] and then returned nothing.
    assert _ids(rows) == ["safe"]
    assert rows[0]["score"] == pytest.approx(0.588, abs=1e-3)


@pytest.mark.asyncio
async def test_tied_scores_give_prefix_stable_top_k(engine_deps, graph_driver, domain_spec, clean_db):
    # Synced out of id order so insertion order cannot explain the result.
    await _sync([_depot(name, 0.80, 100) for name in ("tie-c", "tie-a", "tie-b")])
    serve(graph_driver, _arbitrated(domain_spec))

    top1 = _ids(await _match(top_n=1))
    top2 = _ids(await _match(top_n=2))
    top3 = _ids(await _match(top_n=3))

    assert top3 == ["tie-a", "tie-b", "tie-c"]  # declared identity ascending
    assert top2 == top3[:2]
    assert top1 == top3[:1]
    for _ in range(3):
        assert _ids(await _match(top_n=3)) == top3


@pytest.mark.asyncio
async def test_soft_penalty_never_raises_a_negative_score(
    engine_deps, graph_driver, domain_spec, clean_db, monkeypatch
):
    """SCR-04: with the clamp off a stored negative reliability scores below zero.

    ``score * penalty`` would raise it (-0.2523 * 0.25 = -0.0631, above -0.2523);
    under ``arbitration_unknown_fails_constraint_enabled`` the score is
    ``min(score, score * penalty)`` and stays -0.2523.
    """
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "score_clamp_enabled", False)
    soft = ArbitrationConstraintSpec(dimension="port_capacity", threshold=0.6, hard=False, penalty=0.25)
    await _sync([_depot("negative", -0.50, 2), _depot("positive", 0.50, 2)])
    serve(graph_driver, _arbitrated(domain_spec, soft))

    rows = {row["candidate"]["depot_id"]: row["score"] for row in await _match(top_n=2)}

    assert rows["negative"] == pytest.approx(-0.30 + 0.0477, abs=1e-3)  # unpenalised
    assert rows["positive"] == pytest.approx((0.30 + 0.0477) * 0.25, abs=1e-3)  # penalised
