"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, domain-packs, admission, serving-boundary]
owner: engine-team
status: active
--- /L9_META ---

Serving boundary for the retired legacy matching packs (operator decision
2026-10-25: domain-specific runtime architecture is retired).

The seven legacy packs below declare ``domain.activation: dormant`` and are
withheld by the generic loader admission policy (``withholding_reason``), not
by gate-compile defects. Compiler repairs to those gates must therefore never
widen what is served: under default settings each one is absent from
``list_domains`` and refused by ``load_domain``. The packs that were served
before the dormant marks keep being served.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import engine.config.loader as loader_module
from engine.config.schema import DomainActivation
from engine.config.settings import Settings

DOMAINS = Path(__file__).resolve().parents[2] / "domains"

RETIRED_LEGACY_PACKS = (
    "aios-god-agent",
    "executive-assistant",
    "healthcare-referral",
    "legal-discovery",
    "repo-as-agent",
    "research-agent",
    "roofing-company",
)
# Served under default settings before the dormant marks; must stay served.
SERVED_PACKS = ("freight-matching", "mortgage-brokerage", "plasticos")


@pytest.fixture
def default_admission(monkeypatch: pytest.MonkeyPatch) -> loader_module.DomainPackLoader:
    """A loader over the repository's domains/ with both admission overrides at their defaults."""
    monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", False)
    monkeypatch.setattr(loader_module.settings, "unvalidated_domain_packs_enabled", False)
    return loader_module.DomainPackLoader(config_path=str(DOMAINS))


def test_admission_overrides_default_off() -> None:
    fields = Settings.model_fields
    assert fields["dormant_domain_packs_enabled"].default is False
    assert fields["unvalidated_domain_packs_enabled"].default is False


@pytest.mark.parametrize("pack", RETIRED_LEGACY_PACKS)
def test_retired_pack_declares_dormant(pack: str) -> None:
    """The pack file itself carries the generic dormant declaration; the engine names no domain."""
    raw = yaml.safe_load((DOMAINS / pack / "spec.yaml").read_text(encoding="utf-8"))
    assert raw["domain"]["activation"] == DomainActivation.DORMANT.value


@pytest.mark.parametrize("pack", RETIRED_LEGACY_PACKS)
def test_retired_pack_is_not_listed(pack: str, default_admission: loader_module.DomainPackLoader) -> None:
    assert pack not in default_admission.list_domains()


@pytest.mark.parametrize("pack", RETIRED_LEGACY_PACKS)
def test_retired_pack_is_refused_as_dormant(pack: str, default_admission: loader_module.DomainPackLoader) -> None:
    with pytest.raises(loader_module.DomainNotFoundError, match=r"declared dormant by its pack"):
        default_admission.load_domain(pack)


@pytest.mark.parametrize("pack", SERVED_PACKS)
def test_previously_served_pack_is_unaffected(pack: str, default_admission: loader_module.DomainPackLoader) -> None:
    assert pack in default_admission.list_domains()
    assert default_admission.load_domain(pack).domain.id == pack


def test_served_set_is_exactly_the_previously_served_packs(default_admission: loader_module.DomainPackLoader) -> None:
    """No compiler or pack change may silently add a pack to the default serving surface."""
    assert set(default_admission.list_domains()) == set(SERVED_PACKS)
