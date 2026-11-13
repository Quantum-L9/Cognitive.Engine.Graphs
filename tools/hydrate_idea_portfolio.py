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

from neo4j.exceptions import Neo4jError

from engine.config.loader import DomainNotFoundError, DomainPackLoader
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
from tools.path_policy import PathPolicyError, resolve_existing_file_under_root, resolve_root


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
    """Fail closed unless state_id uniqueness protects the canonical state node.

    MERGE is only single-node-safe when a uniqueness constraint covers the merged
    property, so without one two concurrent initial hydrations can each create a
    canonical state node and each commit a child revision. Runs the existing
    schema-init contract (the same `_init_schema` the `admin`/`init_schema`
    subaction invokes), then verifies the constraint really exists — `_init_schema`
    swallows per-constraint failures, so calling it proves nothing on its own.

    Raises:
        IdeaPortfolioHydrationError: The constraint is absent, or could not be read.
    """
    await _init_schema(driver, spec)
    try:
        rows = await driver.execute_query(SHOW_CONSTRAINTS_CYPHER, {}, database=DOMAIN_ID)
    except Neo4jError as exc:
        # A least-privilege database user may be denied SHOW CONSTRAINTS itself.
        # Unreadable is not provably safe, so it fails closed like an absent
        # constraint — but says so, since the remedy is a grant, not a schema init.
        msg = (
            f"idea-portfolio schema precondition unverifiable: could not read constraints on "
            f"'{DOMAIN_ID}' ({type(exc).__name__}: {exc}). Refusing to mutate. Grant this user "
            f"SHOW CONSTRAINT on the database, or run hydration as a user that holds it."
        )
        raise IdeaPortfolioHydrationError(msg) from exc
    if not state_uniqueness_constraint_present(rows):
        msg = (
            f"idea-portfolio schema precondition unmet: no uniqueness constraint on "
            f"{STATE_LABEL}.{STATE_ID_PROPERTY} after schema init. Concurrent hydration "
            f"could fork the canonical revision chain; refusing to mutate. Run the admin "
            f"init_schema subaction for domain '{DOMAIN_ID}' against this database."
        )
        raise IdeaPortfolioHydrationError(msg)


async def _apply(envelope: IdeaPortfolioHydrationEnvelope) -> dict[str, object]:
    # The pack declares itself dormant (domain.activation: dormant) and the
    # generic loader admission policy withholds it unless the operator opted in
    # with DORMANT_DOMAIN_PACKS_ENABLED=true. Hydration must not outrun that.
    #
    # --apply mutates the configured Neo4j instance, so the spec comes from the
    # pack root that instance's engine serves (DOMAINS_ROOT), exactly as boot
    # resolves it — never from the repository's design packs, which could admit
    # a domain the operator has not published or a different spec version.
    try:
        spec = DomainPackLoader(config_path=str(settings.domains_root)).load_domain(DOMAIN_ID)
    except DomainNotFoundError as exc:
        raise IdeaPortfolioHydrationError(f"idea-portfolio hydration is disabled by configuration: {exc}") from exc
    driver = GraphDriver()
    await driver.connect()
    try:
        await _require_state_uniqueness(driver, spec)
        return await IdeaPortfolioHydrator(driver, enabled=True).apply(envelope)
    finally:
        await driver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or apply IdeaOS -> CEG portfolio hydration")
    parser.add_argument("envelope", type=Path)
    parser.add_argument("--apply", action="store_true", help="mutate the CEG idea-portfolio graph")
    parser.add_argument(
        "--root",
        default=".",
        help="Allowed root for the hydration envelope (default: current directory)",
    )
    args = parser.parse_args(argv)
    try:
        input_root = resolve_root(args.root)
        envelope_path = resolve_existing_file_under_root(args.envelope, input_root, option="envelope")
    except PathPolicyError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        envelope = _load(envelope_path)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result = asyncio.run(_apply(envelope)) if args.apply else _dry_run(envelope)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
