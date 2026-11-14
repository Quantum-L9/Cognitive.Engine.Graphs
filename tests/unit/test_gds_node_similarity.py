"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, gds, node-similarity]
owner: engine-team
status: active
--- /L9_META ---

Unit tests — GDS ``nodeSimilarity`` job (CEG-GDS-SIM).

Covers the plan builder (sanitized identifiers, bounds, parameterization),
scheduler dispatch, tenant database routing, projection cleanup on success
and failure, the flag-off skip, and GDSJobSpec.nodesimilarity validation.
All Neo4j calls are mocked; tests/integration/test_gds_node_similarity.py
proves the edges against a live GDS.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from engine.config.schema import GDSJobScheduleSpec, GDSJobSpec, GDSNodeSimilaritySpec, GDSProjectionSpec
from engine.gds.node_similarity import (
    NODE_SIMILARITY_ALGORITHM,
    NODE_SIMILARITY_ALGORITHMS,
    OWNER_PROPERTY,
    build_node_similarity_plan,
    staging_edge_for,
)
from engine.gds.scheduler import GDSScheduler
from tests.unit.test_gds_scheduler import _minimal_spec

pytestmark = pytest.mark.unit

DB = "test-domain"


def _job(**overrides: Any) -> GDSJobSpec:
    fields: dict[str, Any] = {
        "name": "site_similarity",
        "algorithm": "nodeSimilarity",
        "schedule": GDSJobScheduleSpec(type="manual"),
        "projection": GDSProjectionSpec(nodelabels=["Site", "Query"], edgetypes=["INTERACTED_WITH"]),
        "writeedge": "SIMILAR_TO",
        "writeproperty": "similarity_score",
    }
    fields.update(overrides)
    return GDSJobSpec(**fields)


STAGE = staging_edge_for("site_similarity")


def _driver(write_row: dict[str, Any] | None = None, deleted: int = 3, promoted: int = 6) -> AsyncMock:
    """Answer each statement by what it is, so ordering is asserted separately."""
    row = write_row if write_row is not None else {"nodesCompared": 4, "relationshipsWritten": 6}

    async def _answer(cypher: str, parameters: dict[str, Any] | None = None, database: str | None = None):
        if "gds.nodeSimilarity.write" in cypher:
            return [row]
        if "relationships_deleted, relationships_written" in cypher:
            return [{"relationships_deleted": deleted, "relationships_written": promoted}]
        return []

    driver = AsyncMock()
    driver.execute_query = AsyncMock(side_effect=_answer)
    return driver


@pytest.fixture
def flag_on(monkeypatch):
    from engine.config.settings import settings

    monkeypatch.setattr(settings, "gds_node_similarity_enabled", True)


# ── Plan builder ─────────────────────────────────────────


class TestPlan:
    def test_algorithm_string_matches_spec_vocabulary(self):
        assert NODE_SIMILARITY_ALGORITHM == "nodeSimilarity"

    def test_default_bounds_are_parameters(self):
        plan = build_node_similarity_plan(_job())
        assert plan.config == {
            "writeRelationshipType": STAGE,
            "writeProperty": "similarity_score",
            "topK": 10,
            "degreeCutoff": 1,
            "similarityMetric": "JACCARD",
        }
        assert "similarityCutoff" not in plan.config  # unset keeps the GDS default (1E-42)
        assert plan.params == {
            "graph_name": "site_similarity_graph",
            "job_id": "site_similarity",
            "node_labels": ["Site", "Query"],
            "edge_types": ["INTERACTED_WITH"],
            "config": plan.config,
        }

    def test_declared_bounds_reach_the_config(self):
        bounds = GDSNodeSimilaritySpec(topk=5, similaritycutoff=0.5, degreecutoff=2, similaritymetric="OVERLAP")
        plan = build_node_similarity_plan(_job(nodesimilarity=bounds))
        assert plan.config["topK"] == 5
        assert plan.config["similarityCutoff"] == 0.5
        assert plan.config["degreeCutoff"] == 2
        assert plan.config["similarityMetric"] == "OVERLAP"

    def test_write_statement_interpolates_nothing(self):
        plan = build_node_similarity_plan(_job())
        for cypher in (plan.pre_drop_cypher, plan.project_cypher, plan.write_cypher, plan.drop_cypher):
            assert "SIMILAR_TO" not in cypher
            assert "similarity_score" not in cypher
            assert "Site" not in cypher
        assert "CALL gds.nodeSimilarity.write($graph_name, $config)" in plan.write_cypher
        assert "gds.graph.project($graph_name, $node_labels, $edge_types)" in plan.project_cypher

    def test_gds_writes_into_the_jobs_own_staging_type(self):
        plan = build_node_similarity_plan(_job())
        assert plan.staging_edge == STAGE
        assert plan.config["writeRelationshipType"] == STAGE != "SIMILAR_TO"
        assert staging_edge_for("other_job") != STAGE
        assert staging_edge_for("x" * 500).startswith("GDS_STAGING_")  # bounded for any job name

    def test_swap_deletes_only_relationships_this_job_owns(self):
        plan = build_node_similarity_plan(_job())
        assert "MATCH ()-[old:SIMILAR_TO]->()" in plan.swap_cypher
        assert f"WHERE old.{OWNER_PROPERTY} = $job_id" in plan.swap_cypher
        assert f"MATCH (a)-[s:{STAGE}]->(b)" in plan.swap_cypher
        assert f"r.similarity_score = s.similarity_score, r.{OWNER_PROPERTY} = $job_id" in plan.swap_cypher
        assert "DELETE s" in plan.swap_cypher
        # The staging clear touches only the job's private staging type.
        assert f"MATCH ()-[s:{STAGE}]->()" in plan.clear_staging_cypher
        assert "SIMILAR_TO" not in plan.clear_staging_cypher

    @pytest.mark.parametrize(
        "overrides",
        [
            {"writeedge": "X]->() DETACH DELETE n //"},
            {"writeproperty": "score` SET n.x = 1"},
            {"name": "job name; DROP"},
            {"projection": GDSProjectionSpec(nodelabels=["Site`"], edgetypes=["USES"])},
            {"projection": GDSProjectionSpec(nodelabels=["Site"], edgetypes=["USES ON"])},
        ],
    )
    def test_unsafe_identifiers_are_rejected(self, overrides):
        job = _job(**overrides)
        with pytest.raises(ValueError, match="Invalid label"):
            build_node_similarity_plan(job)

    @pytest.mark.parametrize("missing", ["writeedge", "writeproperty"])
    def test_missing_write_target_fails_explicitly(self, missing):
        job = _job(**{missing: None})
        with pytest.raises(ValueError, match=f"requires {missing!r}"):
            build_node_similarity_plan(job)

    def test_empty_projection_fails_explicitly(self):
        job = _job(projection=GDSProjectionSpec(nodelabels=[], edgetypes=["USES"]))
        with pytest.raises(ValueError, match="requires a projection"):
            build_node_similarity_plan(job)


# ── Scheduler ────────────────────────────────────────────


class TestScheduler:
    def test_flag_default_off(self):
        from engine.config.settings import Settings

        assert Settings.model_fields["gds_node_similarity_enabled"].default is False

    async def test_flag_off_skips_with_reason_and_touches_nothing(self, monkeypatch):
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "gds_node_similarity_enabled", False)
        driver = _driver()
        job = _job()
        result = await GDSScheduler(_minimal_spec([job]), driver).execute_job(job)
        assert result["status"] == "skipped"
        assert "gds_node_similarity_enabled" in result["reason"]
        driver.execute_query.assert_not_called()

    @pytest.mark.parametrize("spelling", ["nodeSimilarity", "node_similarity"])
    async def test_both_spellings_dispatch_to_the_same_job(self, flag_on, spelling):
        driver = _driver()
        job = _job(algorithm=spelling)
        result = await GDSScheduler(_minimal_spec([job], domain_id=DB), driver).execute_job(job)
        assert result["status"] == "success", result
        assert result["algorithm"] == NODE_SIMILARITY_ALGORITHM  # reported under the canonical name
        assert result["relationships_written"] == 6
        writes = [c for c in driver.execute_query.call_args_list if "gds.nodeSimilarity.write" in str(c.args[0])]
        assert len(writes) == 1

    def test_alias_set(self):
        assert NODE_SIMILARITY_ALGORITHMS == {"nodeSimilarity", "node_similarity"}

    async def test_runs_in_order_against_tenant_database(self, flag_on):
        driver = _driver()
        job = _job()
        result = await GDSScheduler(_minimal_spec([job], domain_id=DB), driver).execute_job(job)

        assert result["status"] == "success"
        assert result["algorithm"] == "nodeSimilarity"
        assert result["nodes_compared"] == 4
        assert result["relationships_written"] == 6
        assert result["relationships_deleted"] == 3
        assert result["write_edge"] == "SIMILAR_TO"
        assert result["top_k"] == 10

        calls = driver.execute_query.call_args_list
        statements = [str(c.args[0]) for c in calls]
        markers = [
            "gds.graph.exists",
            "staging_deleted",
            "gds.graph.project",
            "gds.nodeSimilarity.write",
            "relationships_deleted, relationships_written",
            "gds.graph.drop",
        ]
        assert len(statements) == len(markers)
        for statement, marker in zip(statements, markers, strict=True):
            assert marker in statement
        assert all(c.kwargs["database"] == DB for c in calls)
        assert all(c.kwargs["parameters"]["graph_name"] == "site_similarity_graph" for c in calls)

    @pytest.mark.parametrize("failing", ["gds.graph.project", "gds.nodeSimilarity.write"])
    async def test_failure_keeps_previous_result_and_drops_projection(self, flag_on, failing):
        """Codex P1/P2 on #485: nothing deletes the last good output before the
        replacement exists, and a failed or interrupted projection is still dropped."""

        async def _answer(cypher: str, parameters=None, database=None):
            if failing in cypher:
                msg = "procedure failed"
                raise RuntimeError(msg)
            return []

        driver = AsyncMock()
        driver.execute_query = AsyncMock(side_effect=_answer)
        job = _job()
        result = await GDSScheduler(_minimal_spec([job]), driver).execute_job(job)

        assert result["status"] == "failed"
        assert "procedure failed" in result["error"]
        statements = [str(c.args[0]) for c in driver.execute_query.call_args_list]
        assert not any("relationships_deleted, relationships_written" in st for st in statements)
        assert not any("MATCH ()-[old:SIMILAR_TO]" in st for st in statements)
        assert "gds.graph.drop" in statements[-1]

    async def test_invalid_job_fails_before_any_query(self, flag_on):
        driver = _driver()
        job = _job(writeedge=None)
        result = await GDSScheduler(_minimal_spec([job]), driver).execute_job(job)
        assert result["status"] == "failed"
        assert "writeedge" in result["error"]
        driver.execute_query.assert_not_called()

    async def test_unexpected_pre_drop_error_propagates(self, flag_on):
        async def _answer(cypher: str, parameters=None, database=None):
            if "gds.graph.exists" in cypher:
                msg = "connection refused"
                raise RuntimeError(msg)
            return []

        driver = AsyncMock()
        driver.execute_query = AsyncMock(side_effect=_answer)
        job = _job()
        result = await GDSScheduler(_minimal_spec([job]), driver).execute_job(job)
        assert result["status"] == "failed"
        assert driver.execute_query.call_count == 1


# ── Schema ───────────────────────────────────────────────


class TestSchema:
    def test_existing_jobs_validate_unchanged(self):
        job = GDSJobSpec(
            name="communities",
            algorithm="louvain",
            schedule=GDSJobScheduleSpec(type="manual"),
            projection=GDSProjectionSpec(nodelabels=["Site"], edgetypes=["USES"]),
        )
        assert job.nodesimilarity is None

    def test_accepts_valid_bounds_from_yaml_shape(self):
        job = GDSJobSpec.model_validate(
            {
                "name": "j",
                "algorithm": "nodeSimilarity",
                "schedule": {"type": "manual"},
                "projection": {"nodelabels": ["Site"], "edgetypes": ["USES"]},
                "nodesimilarity": {
                    "topk": 100,
                    "similaritycutoff": 1.0,
                    "degreecutoff": 3,
                    "similaritymetric": "COSINE",
                },
            }
        )
        assert job.nodesimilarity == GDSNodeSimilaritySpec(
            topk=100, similaritycutoff=1.0, degreecutoff=3, similaritymetric="COSINE"
        )

    def test_defaults(self):
        bounds = GDSNodeSimilaritySpec()
        assert (bounds.topk, bounds.similaritycutoff, bounds.degreecutoff, bounds.similaritymetric) == (
            10,
            None,
            1,
            "JACCARD",
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"topk": 0},
            {"topk": 101},
            {"similaritycutoff": -0.01},
            {"similaritycutoff": 1.01},
            {"degreecutoff": 0},
            {"similaritymetric": "EUCLIDEAN"},
            {"topK": 5},  # GDS spelling is not the spec spelling; unknown keys are refused
        ],
    )
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(ValidationError):
            GDSNodeSimilaritySpec(**bad)
