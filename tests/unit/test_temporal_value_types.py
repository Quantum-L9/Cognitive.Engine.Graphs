"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync, contract, temporal, gates]
owner: engine-team
status: active
--- /L9_META ---

Declared temporal value types (ceg-scoring-audit L1, D1): native projection,
naive/malformed refusal and the freshness gate's declared-type cutoff.
FEATURE_GATES "Declared temporal value types".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from neo4j.time import Duration

from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.handlers import SyncRowRejectedError, handle_sync, init_dependencies
from engine.state import get_state
from engine.sync.row_contract import SyncRowContract
from tests.fixtures import typed_sync as fx

pytestmark = pytest.mark.unit


def _temporal_raw() -> dict[str, Any]:
    raw = fx.orbital_raw()
    fx.node_props(raw, "OrbitalDepot").extend(
        [
            {"name": "opened_at", "type": "datetime"},
            {"name": "opened_on", "type": "date"},
            {"name": "service_window", "type": "duration"},
            {"name": "inspections", "type": "list", "items": {"type": "date"}},
            {"name": "throughput", "type": "float"},
        ]
    )
    depot = fx.depot_props(raw)
    depot["stocked_grades"]["items"]["properties"].append({"name": "certified_at", "type": "datetime"})
    fx.node_props(raw, "PropellantGrade").append({"name": "certified_at", "type": "datetime"})
    return raw


def _contract(raw: dict[str, Any] | None = None) -> SyncRowContract:
    spec = fx.spec(raw if raw is not None else _temporal_raw())
    return SyncRowContract.from_spec(spec, fx.endpoint(spec, fx.DEPOTS))


def _defects(rows: list[Any]) -> list[str]:
    return _contract().defects(rows, refuse_undeclared=False)


# ── Projection: native temporal values ────────────────────────────────────────


def test_declared_temporals_project_to_driver_native_values() -> None:
    row = {
        "depot_id": "d1",
        "opened_at": "2026-11-11T09:00:00Z",
        "opened_on": "2026-11-11",
        "service_window": "PT36H",
        "inspections": ["2026-01-02", "2026-03-04"],
    }
    rows, dropped = _contract().project([row])
    node = rows[0]["node"]
    assert dropped == {}
    assert node["opened_at"] == datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    assert isinstance(node["opened_at"], datetime)
    assert node["opened_at"].tzinfo is not None  # the driver stores an aware datetime as DateTime
    assert type(node["opened_on"]) is date  # never a datetime: Cypher Date, not LocalDateTime
    assert node["opened_on"] == date(2026, 11, 11)
    assert isinstance(node["service_window"], Duration)
    assert node["service_window"] == Duration(hours=36)
    assert node["inspections"] == [date(2026, 1, 2), date(2026, 3, 4)]
    assert node["depot_id"] == "d1"  # non-temporal values are untouched


@pytest.mark.parametrize(("iso", "hours"), [("2026-11-11T09:00:00+02:00", 2), ("2026-11-11T09:00:00-05:00", -5)])
def test_a_utc_offset_is_kept_as_a_fixed_offset_the_driver_serialises(iso: str, hours: int) -> None:
    value = _contract().project([{"depot_id": "d1", "opened_at": iso}])[0][0]["node"]["opened_at"]
    assert value.tzinfo == timezone(timedelta(hours=hours))  # driver 5.8+: datetime.timezone is native
    assert value == datetime(2026, 11, 11, 9 - hours, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("P2W", Duration(days=14)),
        ("P1Y2M3DT4H5M6.5S", Duration(years=1, months=2, days=3, hours=4, minutes=5, seconds=6.5)),
        ("P1.5Y", Duration(years=1, months=6)),
        ("PT0.5H", Duration(minutes=30)),
    ],
)
def test_every_accepted_iso_duration_becomes_a_native_duration(iso: str, expected: Duration) -> None:
    assert _defects([{"depot_id": "d1", "service_window": iso}]) == []
    rows, _ = _contract().project([{"depot_id": "d1", "service_window": iso}])
    assert rows[0]["node"]["service_window"] == expected


def test_childsync_item_temporals_are_native_too() -> None:
    row = {"depot_id": "d1", "stocked_grades": [{"grade_code": "LOX", "certified_at": "2026-02-01T00:00:00Z"}]}
    rows, _ = _contract().project([row])
    item = rows[0]["children"]["stocked_grades"][0]
    assert item == {"grade_code": "LOX", "certified_at": datetime(2026, 2, 1, tzinfo=UTC)}


def test_null_and_absent_temporals_are_not_invented() -> None:
    rows, _ = _contract().project([{"depot_id": "d1", "opened_at": None}])
    assert rows[0]["node"] == {"depot_id": "d1", "opened_at": None}


def test_plasticos_last_transaction_date_projects_to_a_native_datetime() -> None:
    spec = fx.plasticos()
    contract = SyncRowContract.from_spec(spec, fx.endpoint(spec, "/v1/sync/facilities"))
    row = {"facility_id": "f1", "name": "F", "last_transaction_date": "2026-11-20T12:00:00Z"}
    assert contract.defects([row], refuse_undeclared=False) == []
    rows, _ = contract.project([row])
    assert rows[0]["node"]["last_transaction_date"] == datetime(2026, 11, 20, 12, tzinfo=UTC)


# ── Ingress: malformed, naive and non-finite values are refused ──────────────


# A date-only string parses as a naive midnight: refused, never guessed into UTC.
@pytest.mark.parametrize("naive", ["2026-11-11T09:00:00", "2026-11-11"])
def test_a_timezone_naive_datetime_is_refused_without_echoing_it(naive: str) -> None:
    defects = _defects([{"depot_id": "d1", "opened_at": naive}])
    assert defects == ["row 0 opened_at: expected datetime, got string (timezone-naive ISO-8601 datetime)"]
    assert not any("2026-11-11" in defect for defect in defects)


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("opened_at", "yesterday", "string (not an ISO-8601 datetime)"),
        ("opened_on", "11/11/2026", "string (not an ISO-8601 date)"),
        ("service_window", "36 hours", "string (not an ISO-8601 duration)"),
        ("opened_at", 1731315600, "int"),
    ],
)
def test_malformed_temporals_are_refused(key: str, value: Any, reason: str) -> None:
    declared = {"opened_at": "datetime", "opened_on": "date", "service_window": "duration"}[key]
    assert _defects([{"depot_id": "d1", key: value}]) == [f"row 0 {key}: expected {declared}, got {reason}"]


def test_a_naive_datetime_inside_a_childsync_item_is_refused() -> None:
    row = {"depot_id": "d1", "stocked_grades": [{"grade_code": "LOX", "certified_at": "2026-02-01T00:00:00"}]}
    assert _defects([row]) == [
        "row 0 stocked_grades[0].certified_at: expected datetime, got string (timezone-naive ISO-8601 datetime)"
    ]


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_floats_are_still_refused(value: float) -> None:
    assert _defects([{"depot_id": "d1", "throughput": value}]) == [
        "row 0 throughput: expected float, got float (non-finite)"
    ]


def test_flag_off_restores_the_legacy_string_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_temporal_native_values_enabled", False)
    row = {"depot_id": "d1", "opened_at": "2026-11-11T09:00:00", "opened_on": "2026-11-11"}
    assert _defects([row]) == []  # naive accepted, as before
    rows, _ = _contract().project([row])
    assert rows[0]["node"] == row  # stored as the strings the row carried


# ── handle_sync: the engine's own write path binds native values ─────────────


@pytest.fixture
def driver() -> Iterator[MagicMock]:
    get_state().reset()
    mock = MagicMock()
    mock.execute_query = AsyncMock(return_value=[])
    mock.execute_write = AsyncMock()
    loader = MagicMock()
    loader.load_domain.return_value = fx.spec(_temporal_raw())
    init_dependencies(mock, loader)
    yield mock
    get_state().reset()


async def test_handle_sync_binds_native_temporals_into_the_write(driver: MagicMock) -> None:
    batch = [{"depot_id": "d1", "opened_at": "2026-11-11T09:00:00Z", "opened_on": "2026-11-11"}]
    result = await handle_sync("orbital-logistics", {"entity_type": "orbital-depots", "batch": batch})
    assert result["status"] == "success"
    bound = driver.execute_query.await_args.kwargs["parameters"]["batch"][0]["node"]
    assert bound["opened_at"] == datetime(2026, 11, 11, 9, tzinfo=UTC)
    assert type(bound["opened_on"]) is date
    assert batch[0]["opened_at"] == "2026-11-11T09:00:00Z"  # the caller's row is not mutated


async def test_handle_sync_refuses_a_naive_datetime_before_any_cypher(driver: MagicMock) -> None:
    batch = [{"depot_id": "d1", "opened_at": "2026-11-11T09:00:00"}]
    with pytest.raises(SyncRowRejectedError) as raised:
        await handle_sync("orbital-logistics", {"entity_type": "orbital-depots", "batch": batch})
    assert "timezone-naive" in str(raised.value)
    driver.execute_query.assert_not_awaited()


# ── Freshness gate: compared in the declared temporal type ───────────────────


def _freshness_spec(prop_type: str | None, *, prop: str = "seen") -> DomainSpec:
    raw = yaml.safe_load(
        """
domain: {id: freshness-check, name: Freshness check, version: 0.1.0}
ontology:
  nodes:
    - {label: Widget, managedby: sync, candidate: true, properties: [{name: widget_id, type: string, required: true}]}
    - {label: Ask, managedby: api, queryentity: true, properties: [{name: ask_id, type: string, required: true}]}
  edges: []
matchentities:
  candidate: [{label: Widget, matchdirection: d1}]
  queryentity: [{label: Ask, matchdirection: d1}]
queryschema: {matchdirections: [d1], fields: []}
gates: []
scoring: {dimensions: []}
"""
    )
    if prop_type is not None:
        raw["ontology"]["nodes"][0]["properties"].append({"name": prop, "type": prop_type})
    raw["gates"] = [{"name": "fresh", "type": "freshness", "candidateprop": prop, "maxagedays": 30}]
    return DomainSpec.model_validate(raw)


def _compile_freshness(spec: DomainSpec) -> str:
    from engine.gates.compiler import GateCompiler

    return GateCompiler(spec).compile(spec.gates[0])


# Date >= DateTime is null in Cypher, so a datetime() cutoff failed every date-declared candidate;
# undeclared (updated_at, stamped by datetime()) and datetime-declared keep datetime().
@pytest.mark.parametrize(
    ("declared", "flag", "now"),
    [
        ("date", True, "date()"),
        ("datetime", True, "datetime()"),
        (None, True, "datetime()"),
        ("date", False, "datetime()"),
    ],
)
def test_freshness_compares_in_the_declared_temporal_type(
    declared: str | None, flag: bool, now: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "freshness_gate_declared_temporal_enabled", flag)
    compiled = _compile_freshness(_freshness_spec(declared))
    assert f"candidate.seen >= {now} - duration({{days: 30}})" in compiled
    assert ("datetime()" in compiled) is (now == "datetime()")


def test_an_unscoped_freshness_gate_uses_the_compiled_directions_declared_type() -> None:
    # Codex review on #563: date in d1, datetime in d2 must not fall back to datetime() for d1.
    raw = _freshness_spec("date").model_dump(mode="json", by_alias=True, exclude_none=True)
    raw["ontology"]["nodes"].append(
        {
            "label": "Gadget",
            "managedby": "sync",
            "candidate": True,
            "properties": [{"name": "seen", "type": "datetime"}],
        }
    )
    raw["matchentities"]["candidate"].append({"label": "Gadget", "matchdirection": "d2"})
    raw["matchentities"]["queryentity"].append({"label": "Ask", "matchdirection": "d2"})
    raw["queryschema"]["matchdirections"] = ["d1", "d2"]
    from engine.gates.compiler import GateCompiler

    spec = DomainSpec.model_validate(raw)
    assert "candidate.seen >= date() - duration" in GateCompiler(spec).compile_all_gates("d1")
    assert "candidate.seen >= datetime() - duration" in GateCompiler(spec).compile_all_gates("d2")
