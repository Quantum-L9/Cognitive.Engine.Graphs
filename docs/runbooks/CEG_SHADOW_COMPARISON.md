# Runbook: CEG shadow comparison outputs (TASK-055)

## Purpose
Produce observational primary-vs-shadow ranking JSON for a Gate `packet_id`.

## Emit offline
```bash
env PYTHONPATH=. python3.12 tools/shadow_comparison.py \
  --input /tmp/shadow-input.json \
  --output /tmp/shadow-comparison.json
```

Input shape:
```json
{
  "packet_id": "...",
  "primary": [{"candidate_id": "…", "score": 0.9, "rank": 1}],
  "shadow": [{"candidate_id": "…", "score": 0.8, "rank": 1}]
}
```

## Safety
- Does not modify `handle_match` responses.
- Does not write to Neo4j.
- Treat output as evidence only until TASK-056 integration.

## Rollback
Delete or ignore comparison artifacts; primary match path unchanged.
