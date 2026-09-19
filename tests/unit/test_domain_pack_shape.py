"""CEG-009 — every domain under domains/ must be in the shape the loader reads.

``DomainPackLoader`` resolves ``domains/<domain_id>/spec.yaml``
(``loader.SPEC_FILENAME``). Nine specs used a flat
``<name>_domain_spec.yaml`` convention instead and were therefore unreachable:
they looked like available verticals in a directory listing, but a tenant id
matching one of them failed to resolve. Only ``plasticos`` loaded by default.

Two of those nine were additionally invalid — ``executive-assistant`` declared
an edge to an undeclared ``Skill`` node, ``roofing-company`` one to ``ZipCode``
— and nothing caught it, because a file the loader never opens is never
validated either.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.config.loader import SPEC_FILENAME, DomainPackLoader
from engine.config.schema import DomainSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAINS_ROOT = REPO_ROOT / "domains"


def _domain_dirs() -> list[Path]:
    return sorted(p for p in DOMAINS_ROOT.iterdir() if p.is_dir())


def test_no_flat_domain_spec_files_remain() -> None:
    stragglers = sorted(p.name for p in DOMAINS_ROOT.glob("*_domain_spec.yaml"))
    assert stragglers == [], (
        f"{stragglers} use the flat shape the loader never reads. Move each to "
        f"domains/<domain_id>/{SPEC_FILENAME}, or delete it if superseded."
    )


def test_every_domain_directory_holds_a_spec() -> None:
    missing = [d.name for d in _domain_dirs() if not (d / SPEC_FILENAME).is_file()]
    assert missing == [], f"domain directories without {SPEC_FILENAME}: {missing}"


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_domain_spec_validates(domain_dir: Path) -> None:
    """A spec the loader can find is a spec that must survive validation."""
    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    DomainSpec.model_validate(raw)


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_directory_name_matches_declared_domain_id(domain_dir: Path) -> None:
    """The loader resolves by domain id, so the directory name IS the lookup key."""
    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    assert raw["domain"]["id"] == domain_dir.name


def test_loader_discovers_every_unflagged_domain() -> None:
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    discovered = set(loader.list_domains())
    on_disk = {d.name for d in _domain_dirs()}
    # idea-portfolio is gated by settings.idea_portfolio_enabled and is expected
    # to be absent while that flag is off.
    assert discovered <= on_disk
    assert on_disk - discovered <= {"idea-portfolio"}
    assert len(discovered) >= 10, f"expected the migrated verticals, got {sorted(discovered)}"


def test_every_discovered_domain_actually_loads() -> None:
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    for domain_id in sorted(loader.list_domains()):
        assert loader.load_domain(domain_id).domain.id == domain_id
