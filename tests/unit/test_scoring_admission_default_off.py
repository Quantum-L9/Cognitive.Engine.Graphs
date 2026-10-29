"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-packs, admission, feature-flags]
owner: engine-team
status: active
--- /L9_META ---

RU-020 scoring admission ships off (owner decision 2026-10-29).

Pack withholding of freight-matching and mortgage-brokerage defaults OFF: both
packs keep being served as on main until the operator enables withholding. These
tests assert the shipped default and that turning the check on still withholds
(the mechanism stays covered in test_domain_pack_loader.TestScoringAdmission).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import engine.config.loader as loader_module
from engine.config.settings import Settings

DOMAINS = Path(__file__).resolve().parents[2] / "domains"


@pytest.fixture
def shipped_admission(monkeypatch: pytest.MonkeyPatch) -> loader_module.DomainPackLoader:
    """A loader over the repository's domains/ with the admission settings at their shipped defaults."""
    for name in ("scoring_admission_check_enabled", "dormant_domain_packs_enabled", "unvalidated_domain_packs_enabled"):
        monkeypatch.setattr(loader_module.settings, name, Settings.model_fields[name].default)
    return loader_module.DomainPackLoader(config_path=str(DOMAINS))


@pytest.mark.unit
def test_scoring_admission_check_defaults_off() -> None:
    assert Settings.model_fields["scoring_admission_check_enabled"].default is False


@pytest.mark.unit
@pytest.mark.parametrize("pack", ["freight-matching", "mortgage-brokerage"])
def test_pack_is_served_under_the_shipped_defaults(
    pack: str, shipped_admission: loader_module.DomainPackLoader
) -> None:
    """Served as on main: listed, and load_domain returns it."""
    assert pack in shipped_admission.list_domains()
    assert shipped_admission.load_domain(pack).domain.id == pack


@pytest.mark.unit
def test_enabling_the_check_withholds_freight_for_its_unbound_scoring(
    monkeypatch: pytest.MonkeyPatch, shipped_admission: loader_module.DomainPackLoader
) -> None:
    """The operator's switch: on, freight-matching is withheld naming what nothing binds."""
    monkeypatch.setattr(loader_module.settings, "scoring_admission_check_enabled", True)
    assert "freight-matching" not in shipped_admission.list_domains()
    with pytest.raises(loader_module.DomainNotFoundError, match="admission defect") as excinfo:
        shipped_admission.load_domain("freight-matching")
    for fragment in ("'lat'", "'lon'", "'target_price'"):
        assert fragment in str(excinfo.value)
