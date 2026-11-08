"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, relationship, evidence, provenance, tenancy, poc]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_observed_association_provenance.py
D-9 (#386) without Neo4j: an observed relationship row's ``observed_association``
is checked against CEG's own copy of the tenant spec (``observedfrom``), never
only against the producer's mirror of it (engine/sync/generator.py
``observed_association_defects`` and the handle_sync seam). Every refusal leaves
the graph untouched.

Vocabulary is synthetic, built at test time from tests/fixtures/domains.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config.schema import DomainSpec, SyncEndpointSpec
from engine.config.settings import Settings, settings
from engine.handlers import ObservedAssociationRejectedError, ValidationError, handle_sync, init_dependencies
from engine.state import get_state
from engine.sync.generator import normalize_relationship_rows, observed_association_defects
from tests.unit.test_observed_association_schema import SOURCE, _raw
from tests.unit.test_relationship_sync import _row

pytestmark = pytest.mark.unit

OBSERVED_PATH = "/v1/sync/tended-by"
SECOND_SOURCE: dict[str, Any] = {**SOURCE, "associationtypeid": 8}


def _spec(*, plain_endpoint: bool = False) -> DomainSpec:
    """cheese-aging with namespaced nodes; the relationship endpoint is observed from two associations, or none."""
    raw = _raw(observedfrom=[dict(SOURCE), dict(SECOND_SOURCE)])
    if plain_endpoint:
        del raw["sync"]["endpoints"][-1]["observedfrom"]
    return DomainSpec.model_validate(raw)


def _endpoint(spec: DomainSpec, path: str) -> SyncEndpointSpec:
    return next(e for e in spec.sync.endpoints if e.path == path)  # type: ignore[union-attr]


# ── The check itself ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("source", [SOURCE, SECOND_SOURCE])
@pytest.mark.parametrize("require", [False, True])
def test_a_declared_association_is_accepted(source: dict[str, Any], require: bool) -> None:
    endpoint = _endpoint(_spec(), OBSERVED_PATH)
    assert observed_association_defects(endpoint, [_row(observed_association=dict(source))], require=require) == []


def test_a_missing_key_is_accepted_only_while_not_required() -> None:
    endpoint = _endpoint(_spec(), OBSERVED_PATH)
    assert observed_association_defects(endpoint, [_row()], require=False) == []
    defects = observed_association_defects(endpoint, [_row()], require=True)
    assert len(defects) == 1
    assert "observed_association is required" in defects[0]


@pytest.mark.parametrize("status", ["reported", "inferred"])
def test_a_non_observed_row_needs_no_key_and_may_not_carry_one(status: str) -> None:
    endpoint = _endpoint(_spec(), OBSERVED_PATH)
    assert observed_association_defects(endpoint, [_row(assertion_status=status)], require=True) == []
    defects = observed_association_defects(
        endpoint, [_row(assertion_status=status, observed_association=dict(SOURCE))], require=False
    )
    assert len(defects) == 1
    assert "only with assertion_status observed" in defects[0]


@pytest.mark.parametrize(
    "association",
    [
        # undeclared type id
        {**SOURCE, "associationtypeid": 99},
        # undeclared provider
        {**SOURCE, "provider": "othercrm"},
        # reversed direction
        {**SOURCE, "fromobjecttype": "affineurs", "toobjecttype": "caves"},
    ],
    ids=["undeclared-type-id", "undeclared-provider", "reversed"],
)
@pytest.mark.parametrize("require", [False, True])
def test_an_association_this_endpoint_does_not_declare_is_refused(association: dict[str, Any], require: bool) -> None:
    endpoint = _endpoint(_spec(), OBSERVED_PATH)
    defects = observed_association_defects(endpoint, [_row(observed_association=association)], require=require)
    assert len(defects) == 1
    assert "is not declared by this endpoint's observedfrom" in defects[0]


@pytest.mark.parametrize(
    "association",
    [
        "synthcrm/caves/affineurs/7",
        [SOURCE],
        {k: v for k, v in SOURCE.items() if k != "associationcategory"},
        {**SOURCE, "extra": "x"},
        {**SOURCE, "associationtypeid": "7"},
        {**SOURCE, "associationtypeid": True},
        None,
    ],
    ids=["string", "list", "missing-key", "extra-key", "string-type-id", "bool-type-id", "null"],
)
def test_a_malformed_association_is_refused(association: Any) -> None:
    endpoint = _endpoint(_spec(), OBSERVED_PATH)
    defects = observed_association_defects(endpoint, [_row(observed_association=association)], require=False)
    assert len(defects) == 1
    assert "must be an object with exactly" in defects[0]


def test_an_endpoint_without_observedfrom_accepts_no_key_and_is_otherwise_unchanged() -> None:
    endpoint = _endpoint(_spec(plain_endpoint=True), OBSERVED_PATH)
    assert observed_association_defects(endpoint, [_row()], require=True) == []
    defects = observed_association_defects(endpoint, [_row(observed_association=dict(SOURCE))], require=True)
    assert len(defects) == 1
    assert "is not declared by this endpoint's observedfrom" in defects[0]


def test_node_endpoints_are_never_checked() -> None:
    endpoint = _endpoint(_spec(), "/v1/sync/affineurs")
    assert observed_association_defects(endpoint, [{"affineur_id": "creamery.affineur:7"}], require=True) == []


def test_every_defective_row_is_reported_and_non_mapping_rows_are_left_to_the_row_validators() -> None:
    endpoint = _endpoint(_spec(), OBSERVED_PATH)
    batch: list[Any] = [
        _row(observed_association=dict(SOURCE)),
        _row(evidence_ref="evidence:2"),
        "not-a-row",
        _row(evidence_ref="evidence:3", observed_association={**SOURCE, "associationtypeid": 99}),
    ]
    defects = observed_association_defects(endpoint, batch, require=True)
    assert [d.split(":", 1)[0] for d in defects] == ["row 1", "row 3"]


def test_the_key_is_provenance_and_never_stored_on_the_edge() -> None:
    plain = normalize_relationship_rows([_row()])
    keyed = normalize_relationship_rows([_row(observed_association=dict(SOURCE))])
    assert keyed == plain
    assert "observed_association" not in keyed[0]["props"]


def test_the_requirement_ships_off_until_the_producer_sends_the_key() -> None:
    assert Settings.model_fields["observed_association_required"].default is False


# ── The handle_sync seam: refused before compliance or any graph I/O ───────────


@pytest.fixture
def graph() -> Iterator[MagicMock]:
    get_state().reset()
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    driver.execute_write = AsyncMock(return_value=([], 0))
    loader = MagicMock()
    loader.load_domain.return_value = _spec()
    init_dependencies(driver, loader)
    yield driver
    get_state().reset()


@pytest.fixture
def _relationship_sync_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "relationship_sync_enabled", True)


@pytest.mark.usefixtures("_relationship_sync_on")
@pytest.mark.parametrize(
    ("row", "required", "match"),
    [
        (_row(observed_association={**SOURCE, "associationtypeid": 99}), False, "is not declared"),
        (
            _row(observed_association={**SOURCE, "fromobjecttype": "affineurs", "toobjecttype": "caves"}),
            False,
            "is not declared",
        ),
        (_row(observed_association={"provider": "synthcrm"}), False, "must be an object with exactly"),
        (_row(), True, "observed_association is required"),
    ],
    ids=["mismatched", "reversed", "malformed", "missing-while-required"],
)
async def test_a_refused_row_writes_nothing(
    graph: MagicMock, monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], required: bool, match: str
) -> None:
    monkeypatch.setattr(settings, "observed_association_required", required)
    with pytest.raises(ObservedAssociationRejectedError, match=match) as raised:
        await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [row]})
    # A ValidationError (422); its class name is what a failure packet carries.
    assert isinstance(raised.value, ValidationError)
    assert type(raised.value).__name__ == "ObservedAssociationRejectedError"
    graph.execute_query.assert_not_awaited()
    graph.execute_write.assert_not_awaited()


@pytest.mark.usefixtures("_relationship_sync_on")
@pytest.mark.parametrize("required", [False, True])
async def test_a_matching_row_reaches_the_write(
    graph: MagicMock, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    monkeypatch.setattr(settings, "observed_association_required", required)
    await handle_sync("cheese-aging", {"entity_type": "tended-by", "batch": [_row(observed_association=dict(SOURCE))]})
    graph.execute_write.assert_awaited_once()
