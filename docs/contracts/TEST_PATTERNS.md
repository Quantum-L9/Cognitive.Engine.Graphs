<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts]
owner: platform
status: active
/L9_META -->

<!-- L9_TEMPLATE: true -->
# L9 Test Writing Contract

## Rule 1: Test Instantiation Must Match METHOD_SIGNATURES.md
Before writing any test, check `docs/contracts/METHOD_SIGNATURES.md` for
the exact constructor signature. Do NOT guess.

## Unit Test Pattern (Gates, Scoring, Parameter Resolution)
```python
import pytest
from engine.config.schema import DomainSpec, GateSpec, GateType


@pytest.fixture
def sample_spec() -> DomainSpec:
    """Load a synthetic verification pack — do NOT construct manually.

    Generic engine tests run on the synthetic packs under tests/fixtures/domains
    (orbital-logistics, cheese-aging), whose vocabulary no shipped pack shares,
    so a passing test proves the engine executed the declared contract rather
    than one product pack's conventions. Load a shipped pack (e.g. PlasticOS)
    only in a test that is explicitly about that pack.
    """
    from tests.fixtures.domain_contracts import synthetic_loader

    return synthetic_loader().load_domain("cheese-aging")


def test_gate_compilation(sample_spec: DomainSpec):
    compiler = GateCompiler(sample_spec)  # Match METHOD_SIGNATURES.md
    result = compiler.compile_all_gates("batch_to_cave")
    assert "WHERE" not in result  # compile_all_gates returns the predicate, not a WHERE clause
    assert "candidate." in result
```


## Integration Test Pattern (Full Pipeline)

```python
import pytest
from testcontainers.neo4j import Neo4jContainer


@pytest.fixture(scope="module")
def neo4j():
    with Neo4jContainer("neo4j:5-enterprise") as container:
        yield container


# NEVER mock GraphDriver for integration tests
# ALWAYS use testcontainers-neo4j
```


## BANNED in Tests

```python
# ❌ No mocking Neo4j driver in integration tests
mock_driver = MagicMock(spec=GraphDriver)  # BANNED in integration/

# ❌ No manual DomainSpec construction with wrong fields
spec = DomainSpec(matchentities=...)  # WILL CRASH — wrong field name

# ❌ No importing from engine.api (doesn't exist)
from engine.api.routers.match import match_endpoint  # BANNED
```

```

## Required Coverage (CONTRACT-17)

| Test type | Location | Must cover |
|---|---|---|
| Unit | `tests/unit/` | Gate compilation, scoring math, parameter resolution, null semantics **per gate type** |
| Integration | `tests/integration/` | Full pipeline against `testcontainers-neo4j` — the driver is never mocked |
| Compliance | `tests/compliance/` | Prohibited factors blocked at **compile** time |
| Performance | `tests/performance/` | p95 match latency < 200 ms |
| Validation | `tests/` | Compiled Cypher for the reference spec matches the hand-verified baseline |

Every new function needs at least one test. The hand-verified Cypher baseline is the
regression anchor for the compiler — if it changes, the change is intentional and
reviewed, not incidental.
