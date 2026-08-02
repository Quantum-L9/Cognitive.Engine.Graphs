"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, config, plasticos, authority]
owner: engine-team
status: active
--- /L9_META ---

Duplicate-authority guard for PlasticOS executable domain selection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config.loader import DomainPackLoader

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = ROOT / "domains"
EXECUTABLE = DOMAINS / "plasticos" / "spec.yaml"
FLAT_RIVAL = DOMAINS / "plasticos_domain_spec.yaml"
ARCHIVE_FLAT = ROOT / "docs" / "archive" / "plasticos" / "plasticos_domain_spec.yaml"


def test_executable_plasticos_spec_exists() -> None:
    assert EXECUTABLE.is_file()


def test_flat_domains_rival_removed() -> None:
    assert not FLAT_RIVAL.exists()


def test_archived_flat_spec_is_non_authoritative() -> None:
    assert ARCHIVE_FLAT.is_file()
    head = ARCHIVE_FLAT.read_text(encoding="utf-8")[:400]
    assert "NON-AUTHORITATIVE" in head


def test_domain_pack_loader_resolves_only_directory_spec() -> None:
    loader = DomainPackLoader(config_path=str(DOMAINS))
    domains = loader.list_domains()
    assert domains.count("plasticos") == 1
    spec = loader.load_domain("plasticos")
    assert spec.domain.id == "plasticos"
    resolved = loader._resolve_spec_path("plasticos")
    assert resolved == EXECUTABLE.resolve()


def test_no_second_plasticos_executable_directory() -> None:
    rivals = [
        path
        for path in DOMAINS.rglob("spec.yaml")
        if path.parent.name.lower().startswith("plasticos") and path != EXECUTABLE
    ]
    assert rivals == [], f"unexpected PlasticOS executable rivals: {rivals}"


@pytest.mark.parametrize(
    "archived",
    sorted((ROOT / "docs" / "archive" / "plasticos").glob("*")),
)
def test_archived_materials_marked_non_authoritative(archived: Path) -> None:
    if archived.name == "ARCHIVE_MAP.md" or not archived.is_file():
        return
    head = archived.read_text(encoding="utf-8")[:500]
    assert "NON-AUTHORITATIVE" in head
