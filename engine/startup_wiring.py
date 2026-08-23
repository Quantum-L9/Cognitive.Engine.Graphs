"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [startup, wiring]
owner: engine-team
status: active
--- /L9_META ---

GAP-FIX STARTUP WIRING
Add these calls to your application lifespan / startup handler in order.
This file is a recipe — adapt paths to match your actual app entrypoint.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)


async def apply_all_gap_fixes(pg_dsn: str, neo4j_driver, domain_pack_loader) -> None:
    """
    Call once during application startup, before serving requests.
    Parameters:
        pg_dsn            - asyncpg-compatible DSN string
        neo4j_driver      - AsyncDriver from neo4j-driver
        domain_pack_loader - DomainPackLoader instance
    """

    # ── Gap 5: Wire PostgreSQL audit pool ────────────────────────────────────
    from shared.audit_persistence import configure_audit_pool

    pg_pool = await asyncpg.create_pool(pg_dsn, min_size=2, max_size=10)
    await configure_audit_pool(pg_pool)
    logger.info("startup: Gap-5 audit pool wired")

    # ── Gap 2: Initialise GRAPH→ENRICH return channel ────────────────────────
    from engine.graph_return_channel import GraphToEnrichReturnChannel

    GraphToEnrichReturnChannel.get_instance()
    logger.info("startup: Gap-2 return channel initialised")

    # ── Gap 6: Register community-export hook on GDS scheduler ───────────────
    from graph.community_export import export_community_labels_to_enrich

    try:
        from graph.gds_scheduler import GDSScheduler

        GDSScheduler.register_post_job_hook(
            job_type="louvain",
            hook=lambda tenant_id, domain_id: export_community_labels_to_enrich(neo4j_driver, tenant_id, domain_id),
        )
        logger.info("startup: Gap-6 community export hook registered")
    except ImportError:
        logger.warning("startup: GDSScheduler not found — register Gap-6 hook manually")

    # Gap 9: the removed v1 inference bridge has no startup wiring.
    # Do not add a successor bridge unless a real producer/consumer contract exists.

    logger.info("startup: all gap fixes applied successfully")
