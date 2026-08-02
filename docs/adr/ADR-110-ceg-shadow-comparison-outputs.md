# ADR-110: CEG shadow comparison outputs

## Status
Accepted (TASK-055)

## Context
Wave-8 needs observational comparison between primary Gate match rankings and a shadow scorer without changing production match authority.

## Decision
- Emit structured comparison artifacts via `engine/shadow/` and `tools/shadow_comparison.py`.
- Artifact is observational (`replaces_primary=false`).
- Mismatch classes: `rank`, `score`, `missing`, `extra`.
- Serialization is deterministic for identical inputs (stable checksum).
- Primary `handle_match` remains the authority path; shadow does not replace its response.

## Consequences
Downstream dual-write/shadow validation (TASK-056) consumes these artifacts. No production cutover in this ADR.
