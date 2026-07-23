"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, performance, latency]
owner: engine-team
status: active
--- /L9_META ---

tests/performance/test_query_latency.py
Performance benchmarks for match query latency.
Tests p50/p95/p99 latency against Neo4j with seeded graph data.
Requires: pytest-benchmark or manual timing.

Uses the real engine APIs:
  GateCompiler.compile_all_gates(match_direction) / compile_relaxed(match_direction)
  TraversalAssembler.assemble_traversal(match_direction)
  ScoringAssembler.assemble_scoring_clause(match_direction, weights) -> (clause, meta)
The compiled query binds every gate queryparam and scoring queryprop, mirroring
the parameter backfill performed by handle_match.
"""

from __future__ import annotations

import statistics
import time

import pytest
import structlog

from engine.gates.compiler import GateCompiler
from engine.scoring.assembler import ScoringAssembler
from engine.traversal.assembler import TraversalAssembler

logger = structlog.get_logger(__name__)


def _full_match_params(domain_spec, tenant: str) -> dict:
    """Bind all queryschema fields + scoring queryprops, like handle_match does."""
    params: dict = {
        "tenant": tenant,
        "polymer_type": "HDPE",
        "density": 0.95,
        "mfi": 12.0,
        "contamination_pct": 0.02,
        "requires_food_grade": False,
        "volume_tons": 10.0,
        "lat": 34.05,
        "lon": -118.24,
        "target_price": 800.0,
    }
    # Backfill any remaining queryschema fields and scoring queryprops with null,
    # mirroring the engine's parameter backfill in handle_match.
    for field in domain_spec.queryschema.fields:
        params.setdefault(field.name, None)
    for dim in domain_spec.scoring.dimensions:
        queryprop = getattr(dim, "queryprop", None)
        if queryprop:
            params.setdefault(queryprop, None)
    return params


@pytest.mark.performance
class TestMatchQueryLatency:
    """Benchmark end-to-end match query execution latency."""

    async def _seed_matchable_facilities(self, graph_driver, db: str, tenant: str) -> None:
        """Seed facilities that satisfy the strict gate chain + PROCESSES traversal."""
        await graph_driver.execute_query(
            """
            UNWIND $batch AS row
            MERGE (f:Facility {facility_id: row.facility_id})
            SET f += row, f.tenant = $tenant, f.updated_at = datetime()
            MERGE (p:PolymerFamily {code: 'HDPE'})
            MERGE (f)-[:PROCESSES]->(p)
            """,
            parameters={
                "batch": [
                    {
                        "facility_id": 9001 + i,
                        "name": f"Bench Facility {i}",
                        "min_density": 0.90,
                        "max_density": 1.00,
                        "min_mfi": 5.0,
                        "max_mfi": 20.0,
                        "contamination_tolerance": 0.05,
                        "food_grade_certified": False,
                        "capacity_tons_month": 100.0,
                        "lat": 34.05,
                        "lon": -118.24,
                        "credit_score": 70.0,
                        "reinforcement_score": 0.5,
                    }
                    for i in range(3)
                ],
                "tenant": tenant,
            },
            database=db,
        )

    @pytest.mark.asyncio
    async def test_strict_match_latency_under_100ms(self, graph_driver, seeded_graph, domain_spec):
        """
        Strict match query should complete within 100ms p95 on a
        small seeded graph. Realistic cold-cache benchmark.
        """
        db = seeded_graph["database"]
        tenant = seeded_graph["tenant"]
        await self._seed_matchable_facilities(graph_driver, db, tenant)

        gate_compiler = GateCompiler(domain_spec)
        scoring_assembler = ScoringAssembler(domain_spec)
        traversal_assembler = TraversalAssembler(domain_spec)

        direction = "intake_to_buyer"
        where_clause = gate_compiler.compile_all_gates(direction)
        traversal_clauses = traversal_assembler.assemble_traversal(direction)
        weights = {d.weightkey: d.defaultweight for d in domain_spec.scoring.dimensions}
        scoring_clause, _pareto_meta = scoring_assembler.assemble_scoring_clause(
            direction, weights
        )

        # Build the full Cypher query. The traversal step already introduces
        # `candidate`, so anchor the tenant filter in the WHERE clause.
        traversal_block = (
            "\n".join(traversal_clauses)
            if traversal_clauses
            else "MATCH (candidate:Facility)"
        )
        cypher = f"""
        {traversal_block}
        WHERE candidate.tenant = $tenant AND {where_clause}
        {scoring_clause}
        RETURN candidate.facility_id AS fid, candidate.name AS name, score
        ORDER BY score DESC
        LIMIT 10
        """

        query_params = _full_match_params(domain_spec, tenant)

        # Warmup
        results = await graph_driver.execute_query(
            cypher, parameters=query_params, database=db
        )
        assert results, "Seeded facilities should pass the strict gate chain"

        # Benchmark: 50 iterations
        latencies: list[float] = []
        for _ in range(50):
            t0 = time.perf_counter()
            await graph_driver.execute_query(cypher, parameters=query_params, database=db)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]
        avg = statistics.mean(latencies)

        logger.info(
            "match_query_latency",
            n=50,
            facilities=len(seeded_graph["facility_ids"]),
            avg_ms=round(avg, 1),
            p50_ms=round(p50, 1),
            p95_ms=round(p95, 1),
            p99_ms=round(p99, 1),
        )

        assert p95 < 100, f"p95 latency {p95:.1f}ms exceeds 100ms target"

    @pytest.mark.asyncio
    async def test_relaxed_match_latency_under_200ms(self, graph_driver, seeded_graph, domain_spec):
        """
        Relaxed match (fewer hard gates, more scoring penalties) should
        complete within 200ms p95. Relaxed queries scan more candidates.
        """
        db = seeded_graph["database"]
        tenant = seeded_graph["tenant"]

        gate_compiler = GateCompiler(domain_spec)
        direction = "intake_to_buyer"
        where_clause = gate_compiler.compile_relaxed(direction)

        cypher = f"""
        MATCH (candidate:Facility {{tenant: $tenant}})
        WHERE {where_clause}
        RETURN candidate.facility_id AS fid, candidate.name AS name
        ORDER BY candidate.name
        LIMIT 25
        """

        query_params = _full_match_params(domain_spec, tenant)

        # Warmup
        await graph_driver.execute_query(cypher, parameters=query_params, database=db)

        latencies: list[float] = []
        for _ in range(50):
            t0 = time.perf_counter()
            await graph_driver.execute_query(cypher, parameters=query_params, database=db)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)

        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        avg = statistics.mean(latencies)

        logger.info("relaxed_match_latency", avg_ms=round(avg, 1), p95_ms=round(p95, 1))

        assert p95 < 200, f"p95 latency {p95:.1f}ms exceeds 200ms target"
