# Runbook: CEG outcome replay (TASK-033)

## Input
Odoo artifact from TASK-057 (`schema=l9.odoo.outcome_replay_input.v1`).

## Command
```bash
env PYTHONPATH=. python3.12 tools/outcome_replay.py \
  --input /path/to/odoo-replay-input.json \
  --output /tmp/ceg-outcome-replay.json
```

## Guarantees
- Deterministic: same input → same `outcome_set_hash`
- No Gate / Neo4j / HTTP
- Compatible field set: `tenant`, `action`, `packet_id` (+ optional payload)

## Recovery
Discard generated JSON; revert `engine/replay/` and `tools/outcome_replay.py` if needed.
