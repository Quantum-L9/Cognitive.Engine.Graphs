"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, enrich, feature-flags, sync]
owner: engine-team
status: active
--- /L9_META ---

CEG-ENRICH-GATE: CEG's local ``enrich`` property writer is dormant behind
``enrich_local_writer_enabled`` and, when on, never writes a property the
pack assigns to ``sync``.

Flag off refuses with a typed error naming the flag and touches nothing. Flag
on writes a property no sync endpoint owns, refuses a sync-owned or system
property before any Cypher runs, keeps identifier sanitization and keeps
tenant/database routing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec, SyncStrategy
from engine.config.settings import settings
from engine.handlers import (
    ENRICH_LOCAL_WRITER_FLAG,
    FeatureNotEnabled,
    ValidationError,
    _enrich_sync_owned_properties,
    handle_admin,
    handle_enrich,
    init_dependencies,
)
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

pytestmark = pytest.mark.unit


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        self.calls.append((cypher, parameters, database))
        return [{"enriched_count": 3}]


def _plasticos() -> DomainSpec:
    return DomainPackLoader(config_path=str(DESIGN_PACKS_ROOT)).load_domain("plasticos")


def _wire(spec: DomainSpec) -> tuple[_Capture, MagicMock]:
    driver = _Capture()
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [spec.domain.id]
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver, loader


def _payload(prop: str, expression: str = "1", label: str = "Facility") -> dict[str, Any]:
    return {"entity_type": label, "enrichments": [{"property": prop, "expression": expression}]}


@pytest.fixture
def writer_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enrich_local_writer_enabled", True)


def test_flag_ships_off() -> None:
    assert type(settings).model_fields["enrich_local_writer_enabled"].default is False


async def test_flag_off_refuses_typed_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enrich_local_writer_enabled", False)
    driver, loader = _wire(_plasticos())
    payload = _payload("enrich_note")
    with pytest.raises(FeatureNotEnabled) as exc:
        await handle_enrich("plasticos", payload)
    assert exc.value.flag == ENRICH_LOCAL_WRITER_FLAG == "ENRICH_LOCAL_WRITER_ENABLED"
    assert ENRICH_LOCAL_WRITER_FLAG in str(exc.value)
    assert driver.calls == []
    loader.load_domain.assert_not_called()


@pytest.mark.usefixtures("writer_on")
class TestWriterEnabled:
    async def test_writes_property_no_sync_endpoint_owns(self) -> None:
        spec = _plasticos()
        driver, _ = _wire(spec)
        result = await handle_enrich("plasticos", _payload("enrich_note", "toString(n.name)"))
        assert result == {"status": "success", "enriched_count": 3, "entity_type": "Facility", "tenant": "plasticos"}
        ((cypher, params, database),) = driver.calls
        assert "MATCH (n:Facility)" in cypher
        assert "SET n.enrich_note = toString(n.name)" in cypher
        assert params == {}
        # Tenant/database routing unchanged: the tenant's own spec database.
        assert database == spec.domain.id

    @pytest.mark.parametrize("prop", ["credit_score", "facility_id", "processed_polymers", "community_id"])
    async def test_refuses_sync_owned_property_before_cypher(self, prop: str) -> None:
        driver, _ = _wire(_plasticos())
        payload = _payload(prop)
        with pytest.raises(ValidationError, match="sync endpoint of the pack owns"):
            await handle_enrich("plasticos", payload)
        assert driver.calls == []

    async def test_one_owned_property_refuses_the_whole_call(self) -> None:
        driver, _ = _wire(_plasticos())
        payload = {
            "entity_type": "Facility",
            "enrichments": [
                {"property": "enrich_note", "expression": "1"},
                {"property": "credit_score", "expression": "n.credit_score * 2"},
            ],
        }
        with pytest.raises(ValidationError, match=r"\['credit_score'\]"):
            await handle_enrich("plasticos", payload)
        assert driver.calls == []

    async def test_refuses_system_property(self) -> None:
        driver, _ = _wire(_plasticos())
        payload = _payload("_tenant", "'other'")
        with pytest.raises(ValidationError, match="system properties"):
            await handle_enrich("plasticos", payload)
        assert driver.calls == []

    async def test_label_no_sync_endpoint_writes_owns_nothing(self) -> None:
        spec = _plasticos()
        assert not any(e.targetnode == "MaterialIntake" for e in spec.sync.endpoints)  # type: ignore[union-attr]
        driver, _ = _wire(spec)
        await handle_enrich("plasticos", _payload("intake_note", label="MaterialIntake"))
        assert len(driver.calls) == 1

    async def test_entity_ids_keep_declared_id_filter(self) -> None:
        driver, _ = _wire(_plasticos())
        payload = {**_payload("enrich_note"), "entity_ids": ["F1"]}
        await handle_enrich("plasticos", payload)
        ((cypher, params, _),) = driver.calls
        assert "WHERE n.facility_id IN $entity_ids" in cypher
        assert params == {"entity_ids": ["F1"]}

    @pytest.mark.parametrize(
        "payload",
        [
            _payload("x} DELETE n //"),
            _payload("bad-name"),
            _payload("ok", label="Facility) DETACH DELETE (n"),
        ],
    )
    async def test_identifiers_are_sanitized(self, payload: dict[str, Any]) -> None:
        driver, _ = _wire(_plasticos())
        with pytest.raises(ValueError, match="Invalid label or type"):
            await handle_enrich("plasticos", payload)
        assert driver.calls == []

    async def test_tenant_access_still_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[str, str]] = []

        def _deny(tenant: str, action: str) -> None:
            seen.append((tenant, action))
            msg = "tenant not allowed"
            raise ValidationError(msg, action=action, tenant=tenant)

        monkeypatch.setattr("engine.handlers._validate_tenant_access", _deny)
        driver, _ = _wire(_plasticos())
        payload = _payload("enrich_note")
        with pytest.raises(ValidationError, match="tenant not allowed"):
            await handle_enrich("plasticos", payload)
        assert seen == [("plasticos", "enrich")]
        assert driver.calls == []


class TestSyncOwnership:
    def test_node_endpoint_owns_declared_and_id_properties(self) -> None:
        spec = _plasticos()
        owned = _enrich_sync_owned_properties(spec, "Facility")
        declared = {p.name for n in spec.ontology.nodes if n.label == "Facility" for p in n.properties}
        assert declared <= owned
        assert "facility_id" in owned
        assert "enrich_note" not in owned

    def test_childsync_target_owns_child_properties_without_its_own_endpoint(self) -> None:
        spec = _plasticos()
        assert spec.sync is not None
        endpoints = [e for e in spec.sync.endpoints if e.targetnode != "PolymerFamily"]
        assert any(c.targetnode == "PolymerFamily" for e in endpoints for c in e.childsync or [])
        trimmed = spec.model_copy(update={"sync": spec.sync.model_copy(update={"endpoints": endpoints})})
        assert {"code", "name", "resin_code"} <= _enrich_sync_owned_properties(trimmed, "PolymerFamily")

    def test_fieldsupdated_and_relationship_endpoints(self) -> None:
        spec = _plasticos()
        assert spec.sync is not None
        base = next(e for e in spec.sync.endpoints if e.targetnode == "MaterialForm")
        patch = base.model_copy(update={"targetnode": "MaterialIntake", "fieldsupdated": ["intake_status"]})
        edge = base.model_copy(update={"targetnode": "MaterialIntake", "targetedge": "SOME_EDGE", "idproperty": "e"})
        spec_patch = spec.model_copy(update={"sync": spec.sync.model_copy(update={"endpoints": [patch]})})
        spec_edge = spec.model_copy(update={"sync": spec.sync.model_copy(update={"endpoints": [edge]})})
        assert "intake_status" in _enrich_sync_owned_properties(spec_patch, "MaterialIntake")
        assert _enrich_sync_owned_properties(spec_edge, "MaterialIntake") == frozenset()


class TestUnwindMatchSetOwnership:
    """Codex P2 on #487: ownership follows what generator.py writes for UNWINDMATCHSET."""

    def _spec(self, fieldsupdated: list[str] | None) -> tuple[DomainSpec, str]:
        spec = _plasticos()
        assert spec.sync is not None
        base = next(e for e in spec.sync.endpoints if e.targetnode == "Facility" and not e.targetedge)
        patch = base.model_copy(
            update={
                "batchstrategy": SyncStrategy.UNWINDMATCHSET,
                "fieldsupdated": fieldsupdated,
                "taxonomyedges": None,
                "childsync": None,
            }
        )
        return spec.model_copy(update={"sync": spec.sync.model_copy(update={"endpoints": [patch]})}), base.idproperty

    def test_fieldsupdated_owns_only_written_fields(self) -> None:
        spec, idprop = self._spec(["name"])
        owned = _enrich_sync_owned_properties(spec, "Facility")
        assert owned == frozenset({"name", "updated_at", idprop})
        declared = {p.name for n in spec.ontology.nodes if n.label == "Facility" for p in n.properties}
        unrelated = sorted(declared - owned)
        assert unrelated, "the fixture needs a declared Facility property sync does not write"
        assert unrelated[0] not in owned

    def test_without_fieldsupdated_set_row_owns_every_declared_property(self) -> None:
        spec, idprop = self._spec(None)
        owned = _enrich_sync_owned_properties(spec, "Facility")
        declared = {p.name for n in spec.ontology.nodes if n.label == "Facility" for p in n.properties}
        assert declared | {idprop} <= owned

    async def test_writer_accepts_an_unowned_declared_property_and_refuses_updated_at(self, writer_on) -> None:
        spec, _ = self._spec(["name"])
        driver, _ = _wire(spec)
        declared = {p.name for n in spec.ontology.nodes if n.label == "Facility" for p in n.properties}
        free = sorted(declared - _enrich_sync_owned_properties(spec, "Facility"))[0]
        await handle_enrich("plasticos", _payload(free))
        assert len(driver.calls) == 1
        payload = _payload("updated_at")
        with pytest.raises(ValidationError, match="updated_at"):
            await handle_enrich("plasticos", payload)
        assert len(driver.calls) == 1


async def test_feature_status_reports_the_enrich_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex P2 on #487: operators can confirm the writer's state via admin feature_status."""
    _wire(_plasticos())
    for value in (False, True):
        monkeypatch.setattr(settings, "enrich_local_writer_enabled", value)
        result = await handle_admin("plasticos", {"subaction": "feature_status"})
        assert result["feature_gates"]["enrich_local_writer_enabled"] is value
