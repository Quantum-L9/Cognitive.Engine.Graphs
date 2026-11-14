"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, integration, scoring, communitymatch]
owner: engine-team
status: active
--- /L9_META ---

Integration test — SI-19 / SI-20 communitymatch membership scoring on live Neo4j.

Evaluates the compiled ``soft_match`` communitymatch expression against seeded
candidates whose community ids are 7 (the query's), 8 (adjacent id), 1000
(distant id) and absent. With ``communitymatch_membership_scoring_enabled`` off
the adjacent id earns more than the distant one, an artifact of id arithmetic;
on, both are non-members and score the same, and a member earns the undeclared
bias 1.0.

Requires a live Neo4j; skips when testcontainers / NEO4J_TEST_URI is unavailable.
"""

from __future__ import annotations

import pytest

from engine.config.schema import ScoringDimensionSpec
from engine.config.settings import settings
from engine.scoring.assembler import ScoringAssembler
from engine.utils.security import sanitize_label
from tests.unit.test_hgkr_pass2 import _make_minimal_domain_spec

pytestmark = pytest.mark.integration

LABEL = "SiCommunityCandidate"
PROP = "cluster_ref"
SEED = f"""
UNWIND $rows AS row
CREATE (c:{LABEL} {{cid: row.cid}})
SET c.{PROP} = row.community
"""


def _expr() -> str:
    dim = ScoringDimensionSpec(
        name="membership",
        source="candidateproperty",
        computation="communitymatch",
        candidateprop=PROP,
        queryprop=PROP,
        weightkey="w_membership",
        defaultweight=0.2,
        soft_match=True,
    )
    return ScoringAssembler(_make_minimal_domain_spec(dimensions=[dim]))._compile_communitymatch(dim)


async def _scores(graph_driver) -> dict[str, float]:
    cypher = f"MATCH (candidate:{sanitize_label(LABEL)}) RETURN candidate.cid AS cid, {_expr()} AS score"
    rows = await graph_driver.execute_query(cypher, parameters={PROP: 7}, database="neo4j")
    return {r["cid"]: float(r["score"]) for r in rows}


@pytest.mark.asyncio
async def test_soft_match_scores_membership_not_id_distance(graph_driver, clean_db, monkeypatch) -> None:
    await graph_driver.execute_write(
        cypher=SEED,
        parameters={
            "rows": [
                {"cid": "member", "community": 7},
                {"cid": "adjacent", "community": 8},
                {"cid": "distant", "community": 1000},
                {"cid": "unknown", "community": None},
            ]
        },
        database="neo4j",
    )

    monkeypatch.setattr(settings, "communitymatch_membership_scoring_enabled", False)
    legacy = await _scores(graph_driver)
    assert legacy["member"] == pytest.approx(1.5)
    assert legacy["adjacent"] == pytest.approx(0.75)
    assert legacy["adjacent"] > legacy["distant"]
    assert legacy["unknown"] == pytest.approx(0.5)

    monkeypatch.setattr(settings, "communitymatch_membership_scoring_enabled", True)
    fixed = await _scores(graph_driver)
    assert fixed["member"] == pytest.approx(1.0)
    assert fixed["adjacent"] == pytest.approx(fixed["distant"]) == pytest.approx(0.2)
    assert fixed["unknown"] == pytest.approx(0.5)
