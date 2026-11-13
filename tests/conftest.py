"""
--- L9_META ---
l9_schema: 1
origin: l9-template
engine: graph
layer: [test]
tags: [L9_TEMPLATE, test, fixtures]
owner: platform
status: active
--- /L9_META ---

tests/conftest.py — Shared pytest fixtures.

RULE 9 (L9 Contract): paths MUST be absolute, repo-rooted, never relative to __file__ dir.
RULE 7 (L9 Contract): DomainPackLoader is instantiated with
    DomainPackLoader(config_path=str(DOMAINS_DIR))
    matching DomainPackLoader.__init__(self, config_path: str | None = None).

DOMAINS_DIR is the neutral synthetic pack root, tests/fixtures/domains
(SYNTHETIC_DOMAINS_ROOT in tests/fixtures/domain_contracts.py). The shared
loader, spec, database and engine fixtures run on SYNTHETIC_DOMAIN_ID
(ADR-112 Stage 4, closure finding A16), so integration evidence for the core
does not depend on a product pack.

DESIGN_PACKS_DIR is the repository's design and test packs,
tests/fixtures/design_packs (DESIGN_PACKS_ROOT). Only the explicitly named
``plasticos_*`` / ``design_pack_*`` fixtures read it, for tests that prove
something about that pack itself. Paths are spelled out here so conftest imports
no engine module at collection.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# ── RULE 9: absolute, repo-rooted path — never relative to __file__ dir ───────
DOMAINS_DIR: Path = Path(__file__).parent / "fixtures" / "domains"
DESIGN_PACKS_DIR: Path = Path(__file__).parent / "fixtures" / "design_packs"

# The synthetic tenant every shared fixture runs on. Handlers route queries to
# database=domain_spec.domain.id, so this is also the per-domain database name.
SYNTHETIC_DOMAIN_ID = "orbital-logistics"
PLASTICOS_DOMAIN_ID = "plasticos"


# ── Event Loop ─────────────────────────────────────────────────────────────────
# pytest-asyncio >=1.0 removed support for overriding the `event_loop` fixture.
# Loop sharing is configured in pytest.ini via
# asyncio_default_fixture_loop_scope / asyncio_default_test_loop_scope = session.


# ── Integration requirement guard ──────────────────────────────────────────────
# CI's integration job sets CEG_REQUIRE_INTEGRATION=1 next to real Neo4j and
# PostgreSQL services. Under that flag a missing service is a broken job, not an
# optional test: every "service unavailable" skip becomes a failure, so the job
# can never report green while having exercised nothing. Without the flag
# (local runs, the unit-focused `test` job) the skip behaviour is unchanged.

REQUIRE_INTEGRATION_ENV = "CEG_REQUIRE_INTEGRATION"
INTEGRATION_DIR: Path = Path(__file__).parent / "integration"


def integration_required() -> bool:
    """True when CEG_REQUIRE_INTEGRATION is set to 1/true/yes."""
    return os.getenv(REQUIRE_INTEGRATION_ENV, "").strip().lower() in {"1", "true", "yes"}


def skip_or_fail_unavailable(reason: str) -> None:
    """Skip because an integration dependency is unavailable — or fail if required."""
    if integration_required():
        pytest.fail(f"{REQUIRE_INTEGRATION_ENV}=1 but {reason}", pytrace=False)
    pytest.skip(reason)


def _is_integration_path(path: Path) -> bool:
    return path.resolve().is_relative_to(INTEGRATION_DIR.resolve())


def _fail_required_skip(report: pytest.CollectReport | pytest.TestReport) -> None:
    """Rewrite a skip report under tests/integration into a failure (flag on only)."""
    if hasattr(report, "wasxfail"):
        return  # an explicit xfail is a declared expectation, not a missing service
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    report.outcome = "failed"
    report.longrepr = f"{REQUIRE_INTEGRATION_ENV}=1 forbids skipping integration tests: {reason}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Backstop for skips raised outside neo4j_container (other fixtures, test bodies)."""
    report = yield
    if report.skipped and integration_required() and _is_integration_path(Path(str(item.path))):
        _fail_required_skip(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """Backstop for module-level skips (e.g. pytest.importorskip) under tests/integration."""
    report = yield
    if (
        report.skipped
        and integration_required()
        and isinstance(collector, pytest.Module)
        and _is_integration_path(Path(str(collector.path)))
    ):
        _fail_required_skip(report)
    return report


# ── Test classification guard ─────────────────────────────────────────────────
# `make test` (the required `Analyze (central Core)` check) runs `pytest -m unit`,
# so a test with no classifying marker is silently deselected there. 107 such
# files accumulated before #449-#452 classified them. Every collected test must
# carry one of these markers, so what the required check runs stays deliberate.

CLASSIFYING_MARKERS: frozenset[str] = frozenset({"unit", "integration", "performance"})


def unclassified_nodeids(items: list[Any]) -> list[str]:
    """Node ids of collected tests that carry none of CLASSIFYING_MARKERS."""
    return [item.nodeid for item in items if not any(item.get_closest_marker(name) for name in CLASSIFYING_MARKERS)]


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuse the run when a test is unclassified.

    ``tryfirst`` so it sees the full collection before ``-m`` deselects the very
    tests it exists to catch.
    """
    missing = unclassified_nodeids(items)
    if missing:
        markers = ", ".join(sorted(CLASSIFYING_MARKERS))
        listed = "\n  ".join(missing[:50])
        msg = (
            f"{len(missing)} collected test(s) carry no classifying marker ({markers}); "
            f"`pytest -m unit` would silently skip them. Add pytestmark to the module:\n  {listed}"
        )
        raise pytest.UsageError(msg)


# ── Neo4j Testcontainer ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def neo4j_container():
    """
    Session-scoped Neo4j testcontainer.
    Falls back to NEO4J_TEST_URI env var if testcontainers unavailable.
    With CEG_REQUIRE_INTEGRATION=1, an unavailable Neo4j fails instead of skipping.
    """
    uri = os.getenv("NEO4J_TEST_URI")
    user = os.getenv("NEO4J_TEST_USER", "neo4j")
    password = os.getenv("NEO4J_TEST_PASSWORD", "l9-test-password")

    if uri:
        yield {"uri": uri, "user": user, "password": password}
        return

    try:
        from testcontainers.neo4j import Neo4jContainer

        container = Neo4jContainer(image="neo4j:5.18-enterprise")
        container.with_env("NEO4J_ACCEPT_LICENSE_AGREEMENT", "yes")
        container.with_env("NEO4JLABS_PLUGINS", '["graph-data-science"]')
        container.start()

        yield {
            "uri": container.get_connection_url(),
            "user": "neo4j",
            "password": "password",
        }
        container.stop()

    except ImportError:
        skip_or_fail_unavailable("testcontainers not installed and NEO4J_TEST_URI not set")


# ── Graph Driver ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="session")
async def graph_driver(neo4j_container) -> AsyncGenerator:
    """
    Session-scoped async Neo4j driver.

    Includes a smoke write that proves the driver is live before any test runs.
    If this fails the entire session fails immediately — no false coverage.
    """
    from engine.graph.driver import GraphDriver

    driver = GraphDriver(
        uri=neo4j_container["uri"],
        username=neo4j_container["user"],
        password=neo4j_container["password"],
    )
    await driver.connect()

    # ── Smoke write: proves driver is live before any test runs ────────────────
    result = await driver.execute_write(
        cypher="CREATE (n:_SmokeTest {id: $id}) RETURN n",
        parameters={"id": "smoke-probe"},
        database="neo4j",
    )
    assert result.get("nodes_created", 0) == 1, f"Smoke write failed — Neo4j driver not live. Result: {result}"
    # Clean up smoke node
    await driver.execute_write(
        cypher="MATCH (n:_SmokeTest) DETACH DELETE n",
        parameters={},
        database="neo4j",
    )

    yield driver
    await driver.close()


# ── DomainPackLoader ───────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def domain_loader():
    """Session-scoped domain loader — RULE 7: exact signature."""
    from engine.config.loader import DomainPackLoader

    return DomainPackLoader(config_path=str(DOMAINS_DIR))


# ── Domain Spec ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def domain_spec(domain_loader):
    """Loaded and validated synthetic orbital-logistics DomainSpec."""
    return domain_loader.load_domain(SYNTHETIC_DOMAIN_ID)


@pytest.fixture
def minimal_domain_spec():
    """Minimal in-memory domain spec for unit tests that don't need full YAML."""
    from engine.config.schema import DomainSpec

    raw = {
        "domain": {"id": "test", "name": "Test Domain", "version": "0.0.1"},
        "ontology": {
            "nodes": [
                {
                    "label": "Widget",
                    "managedby": "sync",
                    "candidate": True,
                    "matchdirection": "probe_to_widget",
                    "properties": [
                        {"name": "widget_id", "type": "int", "required": True},
                        {"name": "name", "type": "string"},
                    ],
                },
            ],
            "edges": [],
        },
        "matchentities": {
            "candidate": [{"label": "Widget", "matchdirection": "probe_to_widget"}],
            "queryentity": [{"label": "Widget", "matchdirection": "probe_to_widget"}],
        },
        "queryschema": {"matchdirections": ["probe_to_widget"], "fields": []},
        "traversal": {"steps": []},
        "gates": [],
        "scoring": {"dimensions": []},
    }
    return DomainSpec(**raw)


# ── Per-Domain Database (enterprise multi-db) ────────────────────────────────


async def _create_database(graph_driver, name: str) -> None:
    """CREATE DATABASE (backtick-quoted: synthetic ids carry hyphens). Enterprise only."""
    await graph_driver.execute_query(
        cypher=f"CREATE DATABASE `{name}` IF NOT EXISTS WAIT",
        parameters={},
        database="system",
    )


@pytest_asyncio.fixture(scope="session")
async def domain_database(graph_driver) -> AsyncGenerator[str, None]:
    """Ensure the per-domain database of the synthetic tenant exists; yields its name.

    Handlers route queries to database=domain_spec.domain.id (RULE 7: explicit
    database), so integration tests exercising the synthetic tenant need that
    database to exist. Requires the neo4j enterprise image used by the
    testcontainer fixture (community edition rejects CREATE DATABASE).
    """
    await _create_database(graph_driver, SYNTHETIC_DOMAIN_ID)
    yield SYNTHETIC_DOMAIN_ID


# ── Engine Dependency Injection ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine_deps(graph_driver, domain_loader, domain_database) -> AsyncGenerator[None, None]:
    """Wire the real handler DI (W4-01 EngineState singleton) for handler tests.

    Handlers take (tenant, payload) and resolve driver/loader via
    engine.handlers.init_dependencies() — never as positional arguments.
    Resets EngineState after each test so no state leaks across tests.
    """
    from engine.handlers import init_dependencies
    from engine.state import get_state

    get_state().reset()  # ensure a clean slate even if a prior test initialized state
    init_dependencies(graph_driver, domain_loader)
    yield
    get_state().reset()


# ── Design packs (opt-in, pack-specific tests only) ───────────────────────────
# Never used by default. A test that proves something about a design pack itself
# (not about the core) requests these by name and says why in a comment.


@pytest.fixture(scope="session")
def design_pack_loader():
    """DomainPackLoader over tests/fixtures/design_packs — RULE 7: exact signature."""
    from engine.config.loader import DomainPackLoader

    return DomainPackLoader(config_path=str(DESIGN_PACKS_DIR))


@pytest.fixture(scope="session")
def plasticos_domain_spec(design_pack_loader):
    """The PlasticOS design pack's DomainSpec, for pack-specific tests."""
    return design_pack_loader.load_domain(PLASTICOS_DOMAIN_ID)


@pytest_asyncio.fixture(scope="session")
async def plasticos_database(graph_driver) -> AsyncGenerator[str, None]:
    """Ensure the per-domain 'plasticos' database exists, for pack-specific tests."""
    await _create_database(graph_driver, PLASTICOS_DOMAIN_ID)
    yield PLASTICOS_DOMAIN_ID


@pytest_asyncio.fixture
async def design_pack_deps(graph_driver, design_pack_loader, plasticos_database) -> AsyncGenerator[None, None]:
    """engine_deps over the design packs; wipes the plasticos database after the test."""
    from engine.handlers import init_dependencies
    from engine.state import get_state

    get_state().reset()
    init_dependencies(graph_driver, design_pack_loader)
    yield
    get_state().reset()
    await graph_driver.execute_write(cypher="MATCH (n) DETACH DELETE n", parameters={}, database=PLASTICOS_DOMAIN_ID)


# ── Test Isolation ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=False)
async def clean_db(graph_driver) -> AsyncGenerator[None, None]:
    """Wipe all nodes after each integration test that opts in."""
    yield
    await graph_driver.execute_write(
        cypher="MATCH (n) DETACH DELETE n",
        parameters={},
        database="neo4j",  # RULE 7: explicit database — no implicit default fallback
    )
    # Handlers write to the per-domain database — wipe it too when present.
    try:
        await graph_driver.execute_write(
            cypher="MATCH (n) DETACH DELETE n",
            parameters={},
            database=SYNTHETIC_DOMAIN_ID,
        )
    except Exception:
        pass  # per-domain db not created in this session — nothing to clean


# ── Tenant Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def test_tenant() -> str:
    """Unique tenant ID for test isolation."""
    return f"test-{uuid.uuid4().hex[:8]}"


# ── Seed Data ──────────────────────────────────────────────────────────────────

SEED_DEPOTS = [
    {"depot_id": 1, "name": "Alpha Depot", "reliability": 0.03, "lat": 34.05, "lon": -118.24, "trust_score": 0.7},
    {"depot_id": 2, "name": "Beta Depot", "reliability": 0.05, "lat": 33.77, "lon": -118.19, "trust_score": 0.5},
    {"depot_id": 3, "name": "Gamma Depot", "reliability": 0.10, "lat": 40.71, "lon": -74.01, "trust_score": 0.3},
]


@pytest_asyncio.fixture
async def seeded_graph(graph_driver, test_tenant: str) -> AsyncGenerator[dict[str, Any], None]:
    """Seed deterministic synthetic OrbitalDepot data into Neo4j. Cleans up after test."""
    db = "neo4j"

    await graph_driver.execute_query(
        """
        UNWIND $batch AS row
        MERGE (d:OrbitalDepot {depot_id: row.depot_id})
        SET d += row, d.tenant = $tenant, d.updated_at = datetime()
        """,
        parameters={"batch": SEED_DEPOTS, "tenant": test_tenant},
        database=db,
    )

    yield {
        "tenant": test_tenant,
        "depot_ids": [d["depot_id"] for d in SEED_DEPOTS],
        "database": db,
    }

    await graph_driver.execute_query(
        "MATCH (n {tenant: $tenant}) DETACH DELETE n",
        parameters={"tenant": test_tenant},
        database=db,
    )
