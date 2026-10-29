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
SERVED_PACKS = ("mortgage-brokerage", "plasticos")
# Served before the dormant marks, now withheld by the generic admission policy
# because their match Cypher cannot execute (they failed every match). Each
# entry names what the withholding reason must cite, and the operator flags
# that restore serving (failure recovery of the remediation unit).
WITHHELD_UNTIL_REPAIRED = {
    # RU-020: scoring references $lat/$lon/$target_price nothing binds (CEG-COR-011).
    "freight-matching": (("'lat'", "'lon'", "'target_price'"), ("scoring_admission_check_enabled",)),
}


@pytest.fixture
def default_admission(monkeypatch: pytest.MonkeyPatch) -> loader_module.DomainPackLoader:
    """A loader over the repository's domains/ with both admission overrides at their defaults."""
    monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", False)
    monkeypatch.setattr(loader_module.settings, "unvalidated_domain_packs_enabled", False)
    # The admission hardening checks ship off (owner decision 2026-10-29) and this
    # module asserts what they withhold, so it pins every one that exists on; the
    # test_*_admission_default_off modules assert the shipped defaults.
    for flag in ("scoring_admission_check_enabled", "admission_checks_full_match", "exclusion_endpoint_validation"):
        if flag in Settings.model_fields:
            monkeypatch.setattr(loader_module.settings, flag, True)
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


@pytest.mark.parametrize("pack", sorted(WITHHELD_UNTIL_REPAIRED))
def test_unexecutable_pack_is_withheld_for_its_defects(
    pack: str, default_admission: loader_module.DomainPackLoader
) -> None:
    """Withheld for a named admission defect, not dormancy: the pack itself is active."""
    raw = yaml.safe_load((DOMAINS / pack / "spec.yaml").read_text(encoding="utf-8"))
    assert raw["domain"].get("activation", "active") == "active"
    assert pack not in default_admission.list_domains()
    with pytest.raises(loader_module.DomainNotFoundError, match="admission defect") as excinfo:
        default_admission.load_domain(pack)
    cited, _flags = WITHHELD_UNTIL_REPAIRED[pack]
    for fragment in cited:
        assert fragment in str(excinfo.value)


@pytest.mark.parametrize("pack", sorted(WITHHELD_UNTIL_REPAIRED))
def test_withheld_pack_is_served_again_when_its_checks_are_switched_off(
    pack: str, monkeypatch: pytest.MonkeyPatch, default_admission: loader_module.DomainPackLoader
) -> None:
    _cited, flags = WITHHELD_UNTIL_REPAIRED[pack]
    for flag in flags:
        monkeypatch.setattr(loader_module.settings, flag, False)
    assert default_admission.load_domain(pack).domain.id == pack
