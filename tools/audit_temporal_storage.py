#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tools]
tags: [tools, audit, temporal, read-only]
owner: engine-team
status: active
--- /L9_META ---

Count declared temporal properties stored as STRING in tenant databases (D1).

Read-only: every statement runs in a managed READ transaction
(``engine/health/temporal_storage_audit.py``); nothing is converted. The spec
comes from the pack root the engine serves, resolved by ``DomainPackLoader``
exactly as boot resolves it (``DOMAIN_SPECS_PATH``, then ``DOMAINS_ROOT``).
Exit 0 when no string-valued temporal is found, 2 when any is (conversion
needs a separate approval), 1 on error.

    python tools/audit_temporal_storage.py plasticos [other-tenant ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config.loader import DomainNotFoundError, DomainPackLoader, DomainSpecError
from engine.graph.driver import GraphDriver
from engine.health.temporal_storage_audit import audit_temporal_storage

EXIT_CLEAN = 0
EXIT_ERROR = 1
EXIT_STRING_VALUED = 2


async def _audit(tenants: list[str], domains_root: str | None) -> list[dict[str, Any]]:
    loader = DomainPackLoader(config_path=domains_root)
    specs = [loader.load_domain(tenant) for tenant in tenants]
    driver = GraphDriver()
    await driver.connect()
    try:
        return [await audit_temporal_storage(driver, spec) for spec in specs]
    finally:
        await driver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Count declared temporals stored as STRING (read-only)")
    parser.add_argument("tenants", nargs="+", help="tenant / domain ids to audit")
    parser.add_argument(
        "--domains-root",
        default=None,
        help="pack root (default: the loader's own precedence, DOMAIN_SPECS_PATH then DOMAINS_ROOT)",
    )
    args = parser.parse_args(argv)
    try:
        reports = asyncio.run(_audit(args.tenants, args.domains_root))
    except (DomainNotFoundError, DomainSpecError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps({"reports": reports}, indent=2, sort_keys=True))
    return EXIT_STRING_VALUED if any(report["conversion_required"] for report in reports) else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
