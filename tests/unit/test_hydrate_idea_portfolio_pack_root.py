"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, idea-portfolio, hydration, domain-packs]
owner: engine-team
status: active
--- /L9_META ---

tools/hydrate_idea_portfolio.py loads its spec from the served pack root.

`--apply` mutates the configured Neo4j instance, so the idea-portfolio spec it
initialises the schema from must be the one that instance's engine serves:
Settings.domains_root (DOMAINS_ROOT). It must never be the repository's design
packs, which could admit a domain the operator has not published or a different
spec version (review finding on #475, issue #400).

The loader is replaced by a recorder that refuses the domain, so no Neo4j
connection is opened.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from engine.config.loader import DomainNotFoundError
from engine.sync.idea_portfolio import IdeaPortfolioHydrationError
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT, SYNTHETIC_DOMAINS_ROOT
from tools import hydrate_idea_portfolio as tool

pytestmark = pytest.mark.unit


class _RecordingLoader:
    """Stands in for DomainPackLoader: records the root it is given, admits nothing."""

    roots: list[str | None] = []

    def __init__(self, config_path: str | None = None) -> None:
        type(self).roots.append(config_path)

    def load_domain(self, domain_id: str) -> Any:
        msg = f"Domain '{domain_id}' not published in this pack root"
        raise DomainNotFoundError(msg)


@pytest.fixture
def recorded_roots(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    _RecordingLoader.roots = []
    monkeypatch.setattr(tool, "DomainPackLoader", _RecordingLoader)
    return _RecordingLoader.roots


def test_apply_loads_the_spec_from_the_configured_pack_root(
    recorded_roots: list[str | None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    operator_root = tmp_path / "operator-packs"
    operator_root.mkdir()
    monkeypatch.setattr(tool.settings, "domains_root", operator_root)

    with pytest.raises(IdeaPortfolioHydrationError, match="disabled by configuration"):
        asyncio.run(tool._apply(object()))  # type: ignore[arg-type]

    assert recorded_roots == [str(operator_root)]


def test_apply_never_reads_the_repository_fixture_packs(
    recorded_roots: list[str | None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tool.settings, "domains_root", tmp_path)

    with pytest.raises(IdeaPortfolioHydrationError):
        asyncio.run(tool._apply(object()))  # type: ignore[arg-type]

    fixture_roots = {DESIGN_PACKS_ROOT.resolve(), SYNTHETIC_DOMAINS_ROOT.resolve()}
    assert recorded_roots, "the loader was never constructed"
    for root in recorded_roots:
        assert root is not None
        assert Path(root).resolve() not in fixture_roots
