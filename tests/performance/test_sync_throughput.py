"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, performance, throughput]
owner: engine-team
status: active
--- /L9_META ---

tests/performance/test_sync_throughput.py
Performance benchmarks for batch sync throughput.
Measures entities/second for UNWIND/MERGE sync operations against Neo4j, on
the synthetic orbital-logistics pack's OrbitalDepot (ADR-112 Stage 4, A16).
"""

from __future__ import annotations

import statistics
import time
import uuid

import pytest
import pytest_asyncio
import structlog

from engine.config.schema import SyncEndpointSpec, SyncStrategy
from engine.sync.generator import SyncGenerator

pytestmark = pytest.mark.integration

logger = structlog.get_logger(__name__)


# Production schema setup (admin `init_schema`) gives every node key a
# uniqueness constraint, so sync MERGE/MATCH by id is an index seek. Without an
# index here the benchmark measured a :OrbitalDepot label scan instead, and its
# speed depended on how many OrbitalDepot nodes the integration suite had left in
# the shared database (0.53 s for 500 updates, failing the 1000/sec target).
_BENCH_INDEXES = (
    ("depot_id", "ceg_bench_depot_id"),
    ("tenant", "ceg_bench_depot_tenant"),
    ("_tenant", "ceg_bench_depot_stamp"),
)


async def _ensure_depot_indexes(graph_driver, db: str) -> None:
    """Index :OrbitalDepot(depot_id), :OrbitalDepot(tenant) and :OrbitalDepot(_tenant) unless already indexed.

    Only a RANGE index serves the benchmark's equality MATCH/MERGE (TEXT,
    FULLTEXT, POINT and VECTOR indexes do not). A uniqueness constraint's
    backing index is RANGE, and `CREATE INDEX ... IF NOT EXISTS` can still
    raise for that conflicting constraint, so only a property with no RANGE
    index gets one.
    """
    rows = await graph_driver.execute_query(
        "SHOW INDEXES YIELD type, labelsOrTypes, properties "
        "WHERE type = 'RANGE' AND labelsOrTypes = ['OrbitalDepot'] AND size(properties) = 1 "
        "RETURN properties[0] AS prop",
        database=db,
    )
    indexed = {row["prop"] for row in rows}
    for prop, name in _BENCH_INDEXES:
        if prop not in indexed:
            await graph_driver.execute_query(
                f"CREATE INDEX {name} IF NOT EXISTS FOR (f:OrbitalDepot) ON (f.{prop})",
                database=db,
            )
    await graph_driver.execute_query("CALL db.awaitIndexes(120)", database=db)


@pytest.mark.performance
class TestSyncThroughput:
    """Benchmark batch sync throughput against live Neo4j."""

    @pytest_asyncio.fixture(autouse=True)
    async def _depot_indexes(self, graph_driver) -> None:
        await _ensure_depot_indexes(graph_driver, "neo4j")

    @staticmethod
    def _generate_depot_batch(size: int, tenant: str) -> list[dict]:
        """Generate a batch of synthetic depot entities (declared types: depot_id is a string)."""
        return [
            {
                "depot_id": f"bench-{100_000 + i}",
                "name": f"SyncBench-{i}",
                "lat": 34.0 + (i * 0.001),
                "lon": -118.0 + (i * 0.001),
                "docking_ports": 2 + i % 6,
                "reliability": 0.85 + (i % 10) * 0.01,
                "accepts_hazmat": i % 2 == 0,
                "certifications": ["iso-9001"] if i % 3 == 0 else [],
                "served_orbits": ["LEO"],
                "gate_mode": "strict",
                "tenant": tenant,
            }
            for i in range(size)
        ]

    @pytest.mark.asyncio
    async def test_depot_sync_1000_entities_throughput(self, graph_driver, domain_spec):
        """
        Benchmark: sync 1000 depots via UNWIND/MERGE.
        Target: >500 entities/sec on local Neo4j.
        """
        db = "neo4j"
        tenant = f"syncbench-{uuid.uuid4().hex[:6]}"
        batch_size = 1000

        batch = self._generate_depot_batch(batch_size, tenant)

        # Build the UNWIND/MERGE Cypher
        sync_spec = SyncEndpointSpec(
            path="/v1/sync/orbital-depots",
            method="POST",
            targetnode="OrbitalDepot",
            idproperty="depot_id",
            batchstrategy=SyncStrategy.UNWINDMERGE,
        )
        generator = SyncGenerator(domain_spec)
        cypher = generator.generate_sync_query(sync_spec, batch)
        # The statement reads the declared projection (`row.node`), exactly as
        # handle_sync binds it; undeclared keys (`tenant`, `gate_mode`, ...) are
        # dropped, so this benchmark identifies its rows by the engine stamp
        # `_tenant`, which the statement sets from $tenant.
        write_batch, _dropped = generator.project_batch(sync_spec, batch)

        # Benchmark: 5 iterations
        latencies: list[float] = []
        for _ in range(5):
            # Clean before each iteration
            await graph_driver.execute_query(
                "MATCH (f:OrbitalDepot {_tenant: $tenant}) DETACH DELETE f",
                parameters={"tenant": tenant},
                database=db,
            )

            t0 = time.perf_counter()
            await graph_driver.execute_query(
                cypher,
                parameters={"batch": write_batch, "tenant": tenant},
                database=db,
            )
            elapsed = time.perf_counter() - t0
            latencies.append(elapsed)

        avg_sec = statistics.mean(latencies)
        throughput = batch_size / avg_sec

        logger.info(
            "sync_throughput",
            depots=batch_size,
            avg_sec=round(avg_sec, 2),
            entities_per_sec=round(throughput),
            min_sec=round(min(latencies), 2),
            max_sec=round(max(latencies), 2),
        )

        # Verify data landed
        count_result = await graph_driver.execute_query(
            "MATCH (f:OrbitalDepot {_tenant: $tenant}) RETURN count(f) AS cnt",
            parameters={"tenant": tenant},
            database=db,
        )
        assert count_result[0]["cnt"] == batch_size

        assert throughput > 500, f"Throughput {throughput:.0f} entities/sec below 500 target"

        # Cleanup
        await graph_driver.execute_query(
            "MATCH (f:OrbitalDepot {_tenant: $tenant}) DETACH DELETE f",
            parameters={"tenant": tenant},
            database=db,
        )

    @pytest.mark.asyncio
    async def test_incremental_update_throughput(self, graph_driver, domain_spec):
        """
        Benchmark: UNWIND/MATCH/SET for incremental field updates.
        Updates 500 depots with new reliability values.
        """
        db = "neo4j"
        tenant = f"updatebench-{uuid.uuid4().hex[:6]}"
        batch_size = 500

        # Seed initial data
        batch = self._generate_depot_batch(batch_size, tenant)
        seed_cypher = """
        UNWIND $batch AS row
        MERGE (f:OrbitalDepot {depot_id: row.depot_id})
        SET f += row
        """
        await graph_driver.execute_query(seed_cypher, parameters={"batch": batch}, database=db)

        # Build update batch
        update_batch = [{"depot_id": f"bench-{100_000 + i}", "reliability": 0.07} for i in range(batch_size)]

        update_cypher = """
        UNWIND $batch AS row
        MATCH (f:OrbitalDepot {depot_id: row.depot_id})
        SET f.reliability = row.reliability,
            f.updated_at = datetime()
        """

        latencies: list[float] = []
        for _ in range(5):
            t0 = time.perf_counter()
            await graph_driver.execute_query(update_cypher, parameters={"batch": update_batch}, database=db)
            latencies.append(time.perf_counter() - t0)

        avg_sec = statistics.mean(latencies)
        throughput = batch_size / avg_sec

        logger.info(
            "incremental_update_throughput",
            depots=batch_size,
            avg_sec=round(avg_sec, 2),
            updates_per_sec=round(throughput),
        )

        # Verify update applied
        result = await graph_driver.execute_query(
            """
            MATCH (f:OrbitalDepot {tenant: $tenant})
            WHERE f.reliability = 0.07
            RETURN count(f) AS cnt
            """,
            parameters={"tenant": tenant},
            database=db,
        )
        assert result[0]["cnt"] == batch_size

        assert throughput > 1000, f"Update throughput {throughput:.0f}/sec below 1000 target"

        await graph_driver.execute_query(
            "MATCH (f:OrbitalDepot {tenant: $tenant}) DETACH DELETE f",
            parameters={"tenant": tenant},
            database=db,
        )
