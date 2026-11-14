"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, admin, tenant-isolation]
owner: engine-team
status: active
--- /L9_META ---

RU-003 / CEG-COR-003: every admin subaction's domain is the packet tenant's.

``handle_admin`` read ``domain_id`` from the payload and used it for
load_domain and the database of every query, so an allowlisted tenant could
read, write, schedule or erase in any other tenant's domain. The identity
contract binds tenant and domain id (as HEALTH-05 does for health).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.handlers import ValidationError, handle_admin, init_dependencies
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

DOMAINS = DESIGN_PACKS_ROOT
TENANT = "freight-matching"
OTHER = "plasticos"

# subaction -> extra payload keys (besides domain_id) needed to reach the domain read
DOMAIN_SUBACTIONS: dict[str, dict[str, Any]] = {
    "get_domain": {},
    "init_schema": {},
    "trigger_gds": {"job_name": "j"},
    "calibration_run": {},
    "score_feedback": {},
    "generate_calibration_pairs": {},
    "domain_density_report": {},
    "auto_tune": {},
    "discover_weights": {},
    "trigger_kge": {},
    "kge_status": {},
    "erase_subject": {"data_subject_id": "ds-1"},
    "gds_status": {},
    "gds_trigger": {"job_name": "j"},
    "gds_health": {},
    "emit_inference_feedback": {"entity": {}, "entity_id": "e1"},
    "evaluate": {"test_set": [{"query": {}, "match_direction": "x", "expected_ids": ["a"]}]},
}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_query(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return []


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[_Recorder, MagicMock]:
    # Reach each subaction's domain read: open the gates that precede it.
    for flag in (
        "feedback_enabled",
        "pareto_weight_discovery_enabled",
        "kge_enabled",
        "gdpr_erasure_enabled",
        "graph_inference_feedback_enabled",
    ):
        monkeypatch.setattr(settings, flag, True)
    driver = _Recorder()
    loader = MagicMock()
    init_dependencies(driver, loader)  # type: ignore[arg-type]
    return driver, loader


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-003")
@pytest.mark.parametrize("subaction", sorted(DOMAIN_SUBACTIONS))
async def test_cross_tenant_domain_is_rejected_before_any_access(subaction: str, wired) -> None:
    driver, loader = wired
    payload = {"subaction": subaction, "domain_id": OTHER, **DOMAIN_SUBACTIONS[subaction]}
    with pytest.raises(ValidationError, match=r"does not match the packet tenant 'freight-matching'"):
        await handle_admin(TENANT, payload)
    assert driver.calls == []
    loaded = [c.args[0] for c in loader.load_domain.call_args_list]
    loaded += [c.args[0] for c in loader.load_domain_revision.call_args_list]
    assert OTHER not in loaded


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-003")
async def test_same_tenant_domain_is_served(wired) -> None:
    driver, loader = wired
    served = DomainPackLoader(config_path=str(DOMAINS)).load_domain_revision(TENANT)
    loader.load_domain.return_value = served[0]
    loader.load_domain_revision.return_value = served
    result = await handle_admin(TENANT, {"subaction": "get_domain", "domain_id": TENANT})
    assert result["domain"]["domain"]["id"] == TENANT
    result = await handle_admin(TENANT, {"subaction": "domain_density_report", "domain_id": TENANT})
    assert result["domain_id"] == TENANT
    assert driver.calls[-1]["database"] == TENANT


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-003")
async def test_optional_domain_defaults_to_tenant(wired) -> None:
    driver, loader = wired
    # RU-066 reads compliance.gdpr_subject_id_field; None selects the default field.
    loader.load_domain.return_value = MagicMock(compliance=None)
    result = await handle_admin(TENANT, {"subaction": "erase_subject", "data_subject_id": "ds-1"})
    assert result["status"] == "dry_run"
    assert driver.calls[0]["database"] == TENANT


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-003")
async def test_evaluate_matches_as_the_packet_tenant(wired, monkeypatch: pytest.MonkeyPatch) -> None:
    from engine import handlers

    seen: list[str] = []

    async def _fake_match(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(tenant)
        return {"candidates": []}

    monkeypatch.setattr(handlers, "handle_match", _fake_match)
    _driver, loader = wired
    loader.load_domain.return_value = MagicMock()
    await handle_admin(TENANT, {"subaction": "evaluate", "domain_id": TENANT, **DOMAIN_SUBACTIONS["evaluate"]})
    assert seen == [TENANT]


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-003")
async def test_flag_off_restores_caller_chosen_domain(wired, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_tenant_bound_domain", False)
    driver, loader = wired
    loader.load_domain.return_value = MagicMock()
    await handle_admin(TENANT, {"subaction": "domain_density_report", "domain_id": OTHER})
    assert loader.load_domain.call_args.args[0] == OTHER
    assert driver.calls[0]["database"] == loader.load_domain.return_value.domain.id
