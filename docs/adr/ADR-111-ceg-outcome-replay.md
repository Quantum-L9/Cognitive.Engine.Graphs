# ADR-111: CEG deterministic outcome replay

## Status
Accepted (TASK-033)

## Context
TASK-057 produces Odoo replay inputs (`l9.odoo.outcome_replay_input.v1`). CEG needs an offline consumer that yields identical outcome hashes across runs without Gate or Neo4j.

## Decision
- Accept Odoo schema `l9.odoo.outcome_replay_input.v1` only
- Reject `gate_mutation=true`
- Emit `l9.ceg.outcome_replay.v1` with per-event `outcome_hash` and aggregate `outcome_set_hash`
- Pure function / CLI: `gate_calls=0`, `network=false`
- No handler registration changes; observational tooling only

## Consequences
TASK-058 can chain Odoo input hash → CEG `outcome_set_hash`. Live stack is out of scope for this ADR.
