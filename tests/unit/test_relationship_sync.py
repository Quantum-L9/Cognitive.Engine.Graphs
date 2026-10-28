"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, relationship, evidence, scoring, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_relationship_sync.py
Unit tests for QM.POC0.CEG.GRAPH_EVIDENCE.002A's write side without Neo4j:
relationship sync declaration resolution and row admission
(engine/sync/generator.py) and the handle_sync seam. The binding's failure modes
are tests/unit/test_graph_evidence_binding.py; the real-graph proof is
tests/integration/test_graph_evidence_score.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from neo4j.exceptions import ConstraintError

import engine.handlers as handlers_module
from chassis.errors import FeatureNotEnabled
from engine.config.schema import DomainSpec, SyncEndpointSpec
from engine.config.settings import Settings, settings
from engine.handlers import ExecutionError, ValidationError, handle_sync, init_dependencies
from engine.state import get_state
from engine.sync.generator import (
    MAX_RELATIONSHIP_BATCH,
    normalize_relationship_rows,
    relationship_merge_query,
    relationship_precheck_query,
    relationship_uniqueness_constraint_query,
    resolve_relationship_declaration,
)
from tests.fixtures.domain_contracts import SYNTHETIC_DOMAINS_ROOT

pytestmark = pytest.mark.unit

EDGE_ENDPOINTS = {
    "cheese-aging": [
        {"path": "/v1/sync/affineurs", "targetnode": "Affineur", "idproperty": "affineur_id"},
        {"path": "/v1/sync/tended-by", "targetedge": "TENDED_BY", "fromnode": "AgingCave", "tonode": "Affineur"},
    ],
    "orbital-logistics": [
        {"path": "/v1/sync/stocks", "targetedge": "STOCKS", "fromnode": "OrbitalDepot", "tonode": "PropellantGrade"},
    ],
}


def _spec(domain_id: str, extra: list[dict[str, Any]] | None = None) -> DomainSpec:
    raw = yaml.safe_load((SYNTHETIC_DOMAINS_ROOT / domain_id / "spec.yaml").read_text())
    for endpoint in EDGE_ENDPOINTS[domain_id] + (extra or []):
        raw["sync"]["endpoints"].append({"method": "POST", "batchstrategy": "UNWINDMERGE", **endpoint})
    return DomainSpec(**raw)


def _endpoint(spec: DomainSpec, path: str) -> SyncEndpointSpec:
    return next(e for e in spec.sync.endpoints if e.path == path)  # type: ignore[union-attr]


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "from_id": "creamery.cave:101",
        "to_id": "creamery.affineur:7",
        "evidence_ref": "evidence:1",
        "source_ref": "source:1",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
    }
    row.update(overrides)
    return row


# ── Declaration resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("domain_id", "path", "expected"),
    [
        ("cheese-aging", "/v1/sync/tended-by", ("TENDED_BY", "AgingCave", "cave_id", "Affineur", "affineur_id")),
        (
            "orbital-logistics",
            "/v1/sync/stocks",
            ("STOCKS", "OrbitalDepot", "depot_id", "PropellantGrade", "grade_code"),
        ),
    ],
)
def test_declaration_resolves_from_the_tenant_ontology(domain_id: str, path: str, expected: tuple[str, ...]) -> None:
    spec = _spec(domain_id)
    d = resolve_relationship_declaration(spec, _endpoint(spec, path))
    assert (d.edge_type, d.from_label, d.from_id_property, d.to_label, d.to_id_property) == expected


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        (
            {"path": "/v1/sync/x", "targetedge": "UNDECLARED", "fromnode": "AgingCave", "tonode": "Affineur"},
            "exactly one edge",
        ),
        (
            {"path": "/v1/sync/x", "targetedge": "TENDED_BY", "fromnode": "Affineur", "tonode": "AgingCave"},
            "exactly one edge",
        ),
        (
            {"path": "/v1/sync/x", "targetedge": "SHARES_CULTURE_WITH", "fromnode": "AgingCave", "tonode": "AgingCave"},
            "DIRECTED and managedby sync",
        ),
        ({"path": "/v1/sync/x", "targetedge": "TENDED_BY", "fromnode": "AgingCave"}, "targetedge, fromnode and tonode"),
        (
            {
                "path": "/v1/sync/x",
                "targetedge": "TENDED_BY",
                "fromnode": "AgingCave",
                "tonode": "Affineur",
                "targetnode": "AgingCave",
            },
            "both targetnode and targetedge",
        ),
        (
            {
                "path": "/v1/sync/x",
                "targetedge": "TENDED_BY",
                "fromnode": "AgingCave",
                "tonode": "Affineur",
                "batchstrategy": "UNWINDMATCHSET",
            },
            "UNWINDMERGE",
        ),
    ],
)
def test_undeclared_misdirected_or_ambiguous_endpoints_are_refused(endpoint: dict[str, Any], match: str) -> None:
    spec = _spec("cheese-aging", [endpoint])
    with pytest.raises(ValueError, match=match):
        resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/x"))


def test_conflicting_node_id_declarations_are_refused() -> None:
    spec = _spec("cheese-aging", [{"path": "/v1/sync/affineurs-2", "targetnode": "Affineur", "idproperty": "other_id"}])
    with pytest.raises(ValueError, match="conflicting sync idproperty"):
        resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/tended-by"))


# ── Row admission ──────────────────────────────────────────────────────────────


def test_valid_rows_normalise_to_write_shape() -> None:
    [row] = normalize_relationship_rows([_row(confidence=1, source_revision="rev-1")])
    assert row == {
        "from_id": "creamery.cave:101",
        "to_id": "creamery.affineur:7",
        "evidence_ref": "evidence:1",
        "props": {
            "evidence_ref": "evidence:1",
            "source_ref": "source:1",
            "confidence": 1.0,
            "assertion_status": "observed",
            "polarity": "positive",
            "source_revision": "rev-1",
        },
    }
    assert isinstance(row["props"]["confidence"], float)


def test_identical_rows_deduplicate_and_differing_reuse_is_refused() -> None:
    assert len(normalize_relationship_rows([_row(), _row()])) == 1
    with pytest.raises(ValueError, match="repeated with different content"):
        normalize_relationship_rows([_row(), _row(confidence=0.3)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_ref": ""},
        {"evidence_ref": None},
        {"source_ref": "  "},
        {"confidence": None},
        {"confidence": -0.1},
        {"confidence": 1.01},
        {"confidence": float("nan")},
        {"confidence": True},
        {"confidence": "0.8"},
        {"assertion_status": "rumoured"},
        {"assertion_status": None},
        {"polarity": "neutral"},
        {"from_id": True},
        {"from_id": 1.5},
        {"to_id": ""},
        {"expires_at": "2026-01-01T00:00:00"},
        {"expires_at": "not a date"},
        {"label": "Other"},
        {"relationship_type": "OTHER"},
        {"tenant": "someone-else"},
        {"database": "neo4j"},
    ],
)
def test_invalid_rows_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        normalize_relationship_rows([_row(**overrides)])


@pytest.mark.parametrize(
    "missing", ["from_id", "to_id", "evidence_ref", "source_ref", "confidence", "assertion_status", "polarity"]
)
def test_rows_missing_a_required_field_are_refused(missing: str) -> None:
    row = _row()
    del row[missing]
    with pytest.raises(ValueError, match="missing"):
        normalize_relationship_rows([row])


def test_batch_size_is_bounded() -> None:
    with pytest.raises(ValueError):
        normalize_relationship_rows([])
    rows = [_row(evidence_ref=f"e:{i}") for i in range(MAX_RELATIONSHIP_BATCH + 1)]
    with pytest.raises(ValueError):
        normalize_relationship_rows(rows)


def test_queries_take_structure_from_the_declaration_and_values_only_as_parameters() -> None:
    spec = _spec("orbital-logistics")
    d = resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/stocks"))
    for cypher in (relationship_precheck_query(d), relationship_merge_query(d)):
        assert "OrbitalDepot" in cypher
        assert "PropellantGrade" in cypher
        assert ":STOCKS" in cypher
        assert "$batch" in cypher
        assert "$tenant" in cypher
    merge = relationship_merge_query(d)
    assert "ON CREATE SET" in merge
    assert "ON MATCH" not in merge


# ── Handler seams ──────────────────────────────────────────────────────────────


def test_identity_constraint_is_tenant_scoped_per_declared_type() -> None:
    spec = _spec("orbital-logistics")
    d = resolve_relationship_declaration(spec, _endpoint(spec, "/v1/sync/stocks"))
    assert relationship_uniqueness_constraint_query(d) == (
        "CREATE CONSTRAINT ceg_relationship_evidence_STOCKS IF NOT EXISTS "
        "FOR ()-[r:STOCKS]-() REQUIRE (r._tenant, r.evidence_ref) IS UNIQUE"
    )


@pytest.fixture
def _clean_state() -> Iterator[None]:
    get_state().reset()
    yield
    get_state().reset()


@pytest.fixture
def _relationship_sync_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "relationship_sync_enabled", True)


class _Tx:
    """A managed transaction double: each ``run`` answers the next queued result."""

    def __init__(self, *results: list[dict[str, Any]]) -> None:
        self.statements: list[str] = []
        self._results = list(results)

    async def run(self, cypher: str, _params: dict[str, Any]) -> MagicMock:
        self.statements.append(cypher)
        result = MagicMock()
        result.data = AsyncMock(return_value=self._results.pop(0))
        return result


def _graph(*attempts: _Tx | Exception) -> MagicMock:
    """A GraphDriver double whose ``execute_write`` runs the handler's transaction function."""
    queue = list(attempts)

    async def execute_write(write: Any, *, database: str) -> Any:
        attempt = queue.pop(0)
        if isinstance(attempt, Exception):
            raise attempt
        return await write(attempt)

    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    driver.execute_write = AsyncMock(side_effect=execute_write)
    return driver


def _check(ref: str, *, to_count: int = 1, existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "evidence_ref": ref,
        "from_count": 1,
        "from_any": 1,
        "to_count": to_count,
        "to_any": to_count,
        "existing": existing or [],
    }


def _stored(row: dict[str, Any]) -> dict[str, Any]:
    props = {k: v for k, v in row.items() if k not in ("from_id", "to_id")}
    return {"evidence_ref": row["evidence_ref"], "stored": {**props, "_tenant": "cheese-aging"}}


def _install(driver: MagicMock) -> None:
    loader = MagicMock()
    loader.load_domain.return_value = _spec("cheese-aging")
    init_dependencies(driver, loader)


@pytest.mark.usefixtures("_clean_state", "_relationship_sync_on")
async def test_refused_relationship_batch_never_reaches_the_graph() -> None:
    driver = _graph()
    _install(driver)
    with pytest.raises(ValidationError, match="unsupported keys"):
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row(cypher="MATCH (n) DELETE n")]})
    driver.execute_query.assert_not_awaited()
    driver.execute_write.assert_not_awaited()


@pytest.mark.usefixtures("_clean_state", "_relationship_sync_on")
async def test_missing_endpoint_refuses_the_whole_batch_before_any_write() -> None:
    tx = _Tx([_check("evidence:1"), _check("evidence:2", to_count=0)])
    driver = _graph(tx)
    _install(driver)
    with pytest.raises(ValidationError, match="nothing written"):
        await handle_sync(
            "cheese-aging", {"entity_type": "tended-by", "batch": [_row(), _row(evidence_ref="evidence:2")]}
        )
    assert len(tx.statements) == 1  # the precheck only; no MERGE
    assert "CREATE CONSTRAINT" in driver.execute_query.await_args.kwargs["cypher"]


@pytest.mark.usefixtures("_clean_state", "_relationship_sync_on")
async def test_precheck_write_and_postcheck_share_one_transaction() -> None:
    tx = _Tx([_check("evidence:1")], [_stored(_row())])
    driver = _graph(tx)
    _install(driver)
    result = await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row()]})
    assert result == {
        "status": "success",
        "entity_type": "tended-by",
        "synced_count": 1,
        "created_count": 1,
        "replayed_count": 0,
    }
    assert driver.execute_write.await_count == 1
    assert len(tx.statements) == 2
    assert "OPTIONAL MATCH" in tx.statements[0]
    assert "MERGE" in tx.statements[1]


@pytest.mark.usefixtures("_clean_state", "_relationship_sync_on")
async def test_an_unrecorded_write_raises_inside_the_transaction() -> None:
    tampered = _stored(_row())
    tampered["stored"]["confidence"] = 0.1
    driver = _graph(_Tx([_check("evidence:1")], [tampered]))
    _install(driver)
    with pytest.raises(ExecutionError, match="rolled back"):
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row()]})


@pytest.mark.usefixtures("_clean_state", "_relationship_sync_on")
async def test_a_lost_identity_race_retries_once_and_answers_replay() -> None:
    race = ConstraintError("Neo.ClientError.Schema.ConstraintValidationFailed")
    winner = {"from_id": _row()["from_id"], "to_id": _row()["to_id"], "props": _stored(_row())["stored"]}
    retry = _Tx([_check("evidence:1", existing=[winner])], [_stored(_row())])
    driver = _graph(race, retry)
    _install(driver)
    result = await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row()]})
    assert (result["created_count"], result["replayed_count"]) == (0, 1)
    assert driver.execute_write.await_count == 2


@pytest.mark.usefixtures("_clean_state", "_relationship_sync_on")
async def test_a_second_identity_conflict_is_a_refusal() -> None:
    race = ConstraintError("Neo.ClientError.Schema.ConstraintValidationFailed")
    driver = _graph(race, ConstraintError("Neo.ClientError.Schema.ConstraintValidationFailed"))
    _install(driver)
    with pytest.raises(ValidationError, match="already recorded"):
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row()]})


# ── Activation flag (RELATIONSHIP_SYNC_ENABLED) ────────────────────────────────


def test_relationship_sync_ships_disabled() -> None:
    assert Settings.model_fields["relationship_sync_enabled"].default is False


@pytest.mark.usefixtures("_clean_state")
async def test_flag_off_writes_no_evidence_edge_even_when_the_spec_declares_one() -> None:
    assert settings.relationship_sync_enabled is False
    driver = _graph()
    _install(driver)
    with pytest.raises(FeatureNotEnabled, match="RELATIONSHIP_SYNC_ENABLED"):
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row()]})
    driver.execute_query.assert_not_awaited()
    driver.execute_write.assert_not_awaited()


@pytest.mark.usefixtures("_clean_state")
async def test_flag_off_is_refused_before_compliance_records_the_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    compliance = MagicMock(enabled=True)
    monkeypatch.setattr(handlers_module, "_get_compliance_engine", lambda _spec: compliance)
    _install(_graph())
    with pytest.raises(FeatureNotEnabled):
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row()]})
    compliance.check_sync_request.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True], ids=["flag-off", "flag-on"])
@pytest.mark.usefixtures("_clean_state")
async def test_node_sync_is_unchanged_by_the_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(settings, "relationship_sync_enabled", enabled)
    driver = _graph()
    _install(driver)
    result = await handle_sync("cheese-aging", {"entity_type": "affineurs", "batch": [{"affineur_id": "a:7"}]})
    assert result == {"status": "success", "entity_type": "affineurs", "synced_count": 1}
    assert "MERGE" in driver.execute_query.await_args.kwargs["cypher"]
    driver.execute_write.assert_not_awaited()
