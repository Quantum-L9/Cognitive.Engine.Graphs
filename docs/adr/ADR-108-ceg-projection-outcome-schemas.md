# ADR-108: Compile CEG Projection Sync and Outcome Schemas

**Status:** Accepted
**Task:** TASK-061
**Date:** 2026-08-02

## Decision

CEG is the schema producer for:

- `canonical-projection` — rebuildable Odoo-authority facts (never SoR)
- `sync-projection` — already shipped in TASK-028; kept checksum-aligned
- `outcome-feedback` — idempotent observed business outcomes for calibration

Schemas live under `contracts/payloads/` with positive/negative fixtures.
Native models and `OutcomeFeedbackStore` prove validation and replay.

## Non-goals

- Odoo schema producer (TASK-042)
- Cross-repo parity proof (TASK-062)
- Publishing schemas beyond draft-unpublished

## Acceptance

- Fixture examples validate; unknown outcome types and transport fields reject
- Outcome apply is idempotent by `idempotency_key`
- Odoo remains business authority; CEG projections are rebuildable only
