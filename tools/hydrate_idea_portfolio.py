#!/usr/bin/env python3
"""Validate or apply a revision-chained IdeaOS portfolio hydration envelope."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.sync.idea_portfolio import (
    DOMAIN_ID,
    IdeaPortfolioHydrationEnvelope,
    IdeaPortfolioHydrationError,
    IdeaPortfolioHydrator,
    compile_hydration_plan,
)


def _load(path: Path) -> IdeaPortfolioHydrationEnvelope:
    return IdeaPortfolioHydrationEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _dry_run(envelope: IdeaPortfolioHydrationEnvelope) -> dict[str, object]:
    plan = compile_hydration_plan(envelope)
    return {
        "schema": "ceg.idea-portfolio-hydration-plan/v1",
        "status": "validated",
        "domain": DOMAIN_ID,
        "expected_graph_revision": envelope.expected_graph_revision,
        "batch_digest": plan.batch_digest,
        "graph_revision": plan.graph_revision,
        "records": [{"idea_id": r.resolved_idea_id, "operation": r.operation} for r in envelope.records],
    }


async def _apply(envelope: IdeaPortfolioHydrationEnvelope) -> dict[str, object]:
    if not settings.idea_portfolio_enabled:
        raise IdeaPortfolioHydrationError("idea-portfolio hydration is disabled by configuration")
    DomainPackLoader(config_path=str(ROOT / "domains")).load_domain(DOMAIN_ID)
    driver = GraphDriver()
    await driver.connect()
    try:
        return await IdeaPortfolioHydrator(driver, enabled=True).apply(envelope)
    finally:
        await driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or apply IdeaOS -> CEG portfolio hydration")
    parser.add_argument("envelope", type=Path)
    parser.add_argument("--apply", action="store_true", help="mutate the CEG idea-portfolio graph")
    args = parser.parse_args()
    envelope = _load(args.envelope)
    result = asyncio.run(_apply(envelope)) if args.apply else _dry_run(envelope)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
