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
from engine.config.schema import DomainSpec
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import _init_schema
from engine.sync.idea_portfolio import (
    DOMAIN_ID,
    SHOW_CONSTRAINTS_CYPHER,
    STATE_ID_PROPERTY,
    STATE_LABEL,
    IdeaPortfolioHydrationEnvelope,
    IdeaPortfolioHydrationError,
    IdeaPortfolioHydrator,
    compile_hydration_plan,
    state_uniqueness_constraint_present,
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


async def _require_state_uniqueness(driver: GraphDriver, spec: DomainSpec) -> None:
    """Fail closed unless the canonical state node is protected by a uniqueness constraint.

    The hydrator serialises revisions with `MERGE (state:...{state_id: ...})`, and
    MERGE is only single-node-safe when a uniqueness constraint covers the merged
    property. Without it two concurrent initial hydrations can each create a
    canonical state node and each commit a child revision.

    Runs the repository's existing schema-init contract first — the same
    `_init_schema` the `admin`/`init_schema` subaction invokes, which provisions
    the constraint from the domain ontology's required `state_id` — and then
    verifies the constraint really exists. Verification is not redundant:
    `_init_schema` logs and swallows per-constraint failures, so calling it
    proves nothing on its own.

    Args:
        driver: Connected graph driver.
        spec: Loaded idea-portfolio domain spec.

    Raises:
        IdeaPortfolioHydrationError: The constraint is absent after schema init.
    """
    await _init_schema(driver, spec)
    rows = await driver.execute_query(SHOW_CONSTRAINTS_CYPHER, {}, database=DOMAIN_ID)
    if not state_uniqueness_constraint_present(rows):
        msg = (
            f"idea-portfolio schema precondition unmet: no uniqueness constraint on "
            f"{STATE_LABEL}.{STATE_ID_PROPERTY} after schema init. Concurrent hydration "
            f"could fork the canonical revision chain; refusing to mutate. Run the admin "
            f"init_schema subaction for domain '{DOMAIN_ID}' against this database."
        )
        raise IdeaPortfolioHydrationError(msg)


async def _apply(envelope: IdeaPortfolioHydrationEnvelope) -> dict[str, object]:
    if not settings.idea_portfolio_enabled:
        raise IdeaPortfolioHydrationError("idea-portfolio hydration is disabled by configuration")
    spec = DomainPackLoader(config_path=str(ROOT / "domains")).load_domain(DOMAIN_ID)
    driver = GraphDriver()
    await driver.connect()
    try:
        await _require_state_uniqueness(driver, spec)
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
