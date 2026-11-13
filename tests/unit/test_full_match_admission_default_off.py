"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-packs, admission, gates, feature-flags]
owner: engine-team
status: active
--- /L9_META ---

RU-039 full-match admission and RU-041 exclusion endpoint validation ship off
(owner decision 2026-10-29).

Pack withholding of freight-matching and mortgage-brokerage defaults OFF: both
packs keep being served as on main until the operator enables withholding. These
tests assert the shipped defaults, that mortgage-brokerage is served under them,
and that turning each check on still withholds the pre-repair mortgage shape (the
mechanism suites in test_domain_pack_shape / test_gate_compiler pin the checks on).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import engine.config.loader as loader_module
from engine.config.schema import DomainSpec
from engine.config.settings import Settings
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT

DOMAINS = DESIGN_PACKS_ROOT
_FLAGS = ("admission_checks_full_match", "exclusion_endpoint_validation")
_BLACKLIST = {
    "name": "blacklist_exclusion",
    "type": "exclusion",
    "edgetype": "BLACKLISTEDLENDER",
    "fromnode": "query",
    "tonode": "lender",
}


@pytest.fixture
def shipped_admission(monkeypatch: pytest.MonkeyPatch) -> loader_module.DomainPackLoader:
    """A loader over the repository's tests/fixtures/design_packs/ with every admission setting at its shipped default."""
    for name in (
        *_FLAGS,
        "scoring_admission_check_enabled",
        "dormant_domain_packs_enabled",
        "unvalidated_domain_packs_enabled",
    ):
        if name in Settings.model_fields:
            monkeypatch.setattr(loader_module.settings, name, Settings.model_fields[name].default)
    return loader_module.DomainPackLoader(config_path=str(DOMAINS))


def _pre_repair_mortgage_root(tmp_path: Path) -> Path:
    """mortgage-brokerage as shipped on main: off-grammar lender steps and a query-anchored blacklist."""
    raw = yaml.safe_load((DOMAINS / "mortgage-brokerage" / "spec.yaml").read_text(encoding="utf-8"))
    raw["domain"].pop("activation", None)
    raw["traversal"] = {
        "steps": [
            {"name": "lenderresolve", "pattern": "candidate-[:OFFEREDBY]->lender:Lender", "required": True},
            {
                "name": "lenderstatelicensing",
                "pattern": "lender-[:OPERATESIN]->st:State {statecode: $query.propertystate}",
                "required": True,
            },
        ]
    }
    raw["gates"] = [g for g in raw["gates"] if g["name"] != "blacklist_exclusion"] + [dict(_BLACKLIST)]
    (tmp_path / "mortgage-brokerage").mkdir()
    (tmp_path / "mortgage-brokerage" / "spec.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return tmp_path


@pytest.mark.unit
@pytest.mark.parametrize("flag", _FLAGS)
def test_withholding_check_defaults_off(flag: str) -> None:
    assert Settings.model_fields[flag].default is False


@pytest.mark.unit
def test_mortgage_is_served_under_the_shipped_defaults(shipped_admission: loader_module.DomainPackLoader) -> None:
    assert "mortgage-brokerage" in shipped_admission.list_domains()
    assert shipped_admission.load_domain("mortgage-brokerage").domain.id == "mortgage-brokerage"


@pytest.mark.unit
def test_pre_repair_mortgage_is_served_under_the_shipped_defaults(
    tmp_path: Path, shipped_admission: loader_module.DomainPackLoader
) -> None:
    """The shape main serves stays served while both checks are off."""
    loader = loader_module.DomainPackLoader(config_path=str(_pre_repair_mortgage_root(tmp_path)))
    assert loader.load_domain("mortgage-brokerage").domain.id == "mortgage-brokerage"


@pytest.mark.unit
def test_enabling_full_match_withholds_the_unassemblable_lender_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shipped_admission: loader_module.DomainPackLoader
) -> None:
    monkeypatch.setattr(loader_module.settings, "admission_checks_full_match", True)
    loader = loader_module.DomainPackLoader(config_path=str(_pre_repair_mortgage_root(tmp_path)))
    with pytest.raises(loader_module.DomainNotFoundError, match="admission defect") as excinfo:
        loader.load_domain("mortgage-brokerage")
    assert "lenderresolve" in str(excinfo.value)


@pytest.mark.unit
def test_exclusion_validation_off_compiles_the_legacy_fragment_and_on_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shipped_admission: loader_module.DomainPackLoader
) -> None:
    from engine.gates.compiler import GateCompiler

    raw = yaml.safe_load((_pre_repair_mortgage_root(tmp_path) / "mortgage-brokerage" / "spec.yaml").read_text())
    spec = DomainSpec.model_validate(raw)
    gate = next(g for g in spec.gates if g.name == "blacklist_exclusion")
    assert GateCompiler(spec).compile(gate) == "NOT EXISTS { MATCH (candidate)-[:BLACKLISTEDLENDER]->() }"
    monkeypatch.setattr(loader_module.settings, "exclusion_endpoint_validation", True)
    with pytest.raises(ValueError, match="blacklist_exclusion"):
        GateCompiler(spec).compile(gate)
