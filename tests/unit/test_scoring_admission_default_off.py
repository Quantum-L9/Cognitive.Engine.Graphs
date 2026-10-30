"""RU-020 scoring admission ships off (owner decision 2026-10-29): both packs are served as on main.

The check itself stays covered with it pinned on (test_domain_pack_loader.TestScoringAdmission).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import engine.config.loader as loader_module
from engine.config.settings import Settings

DOMAINS = Path(__file__).resolve().parents[2] / "domains"


@pytest.mark.unit
@pytest.mark.parametrize("pack", ["freight-matching", "mortgage-brokerage"])
def test_pack_is_served_under_the_shipped_default(pack: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["scoring_admission_check_enabled"].default is False
    for name in ("scoring_admission_check_enabled", "dormant_domain_packs_enabled", "unvalidated_domain_packs_enabled"):
        monkeypatch.setattr(loader_module.settings, name, Settings.model_fields[name].default)
    loader = loader_module.DomainPackLoader(config_path=str(DOMAINS))
    assert pack in loader.list_domains()
    assert loader.load_domain(pack).domain.id == pack
