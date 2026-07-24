<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [audit]
tags: [auditors, remediation, performance]
owner: engine-team
status: active
/L9_META -->

# Remediation: `query_performance` auditor

Fix procedure for findings emitted by `tools/auditors/query_performance.py`.
Reproduce with: `python tools/audit_dispatch.py --auditor query_performance`

```
task: Fix query performance finding "<finding_code>"
tier: 1
contracts_to_read:
  - docs/contracts/CYPHER_SAFETY.md
  - docs/contracts/METHOD_SIGNATURES.md
```

## Steps by Category

### N_PLUS_ONE (HIGH, rule A)
A query method (`run`, `execute`, `execute_query`, `execute_read`, `execute_write`,
`search`, `browse`, `read`) is called inside a loop — one round trip per iteration.

- Hoist the query out of the loop and pass the whole collection as a parameter.
- In Cypher, use `UNWIND $batch AS row` to process the set in a single statement.
- This is contract 13: gate-then-score happens in Cypher, not by iterating in Python.

### UNBOUNDED_QUERY (MEDIUM, rule B)
A Cypher `MATCH` reaches a `RETURN` with no `LIMIT` — result size is caller-controlled.

- Add `LIMIT $limit` and resolve `$limit` from settings, never by interpolation.
- For paging, add `SKIP $offset` alongside `LIMIT $limit`.
- Aggregations using `count(...)` are already exempt and will not be flagged.

### STR_COLLECTION (HIGH, rule C)
`str()` was called on a list or dict literal, which produces a Python repr
(single quotes, `None`, `True`) rather than valid JSON or Cypher.

- Use `json.dumps(collection)` when a serialized string is genuinely required.
- Prefer passing the collection as a query parameter (`$batch`) so the driver
  handles encoding — this is the contract-9 path and avoids the problem entirely.

## Anti-Patterns

- DO NOT silence a finding by moving the query into a helper that is still
  called from the loop — the round trips remain.
- DO NOT add `LIMIT 25` as a literal; bound values come from settings.
- DO NOT swap `str()` for an f-string — that reintroduces the same repr bug.

## Acceptance

- [ ] Finding gone from `python tools/audit_dispatch.py --auditor query_performance`
- [ ] No new findings introduced in other auditors
- [ ] `make test` passes
- [ ] `make agent-check` exits 0
