<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, ci, pr-fast, docs]
owner: engine-team
status: active
/L9_META -->

# `tests/pr_fast.d/` — pull-request test selection (make-v2 `test-fast`)

Under the Core `make-v2` facade the required check runs `make test-fast` on
pull requests, merge groups and pushes; `make test` stays the whole unit suite
for local and nightly runs. Core's pull-request expense judge accepts
`test-fast` only as a **literal, bounded** selection: a path list built at run
time (`$(shell …)`, `$(wildcard …)`, or a script reading a file) is
`UNKNOWN_SCRIPT` and blocks the check.

So each campaign lane owns one Make fragment here that appends literal paths:

```make
PR_FAST_TESTS += tests/unit/test_example.py
```

## Adding a lane

1. Create `tests/pr_fast.d/<lane>.mk` with an L9_META header and one
   `PR_FAST_TESTS += <path>` line per test module. Paths are relative to the
   repository root and select `-m unit` tests only (no Docker, no Neo4j).
2. Add one line to `Repo.mk` under `include tests/pr_fast.d/ceg-w.mk`:
   `include tests/pr_fast.d/<lane>.mk`. The include path must be literal.
3. Edit no other lane's fragment.

## Behaviour

- `repo-test-fast` fails before pytest runs when the selection is empty or a
  listed path does not exist.
- A path listed by two lanes is collected once (pytest de-duplicates).
- Run it locally with `make test-fast`.

| Lane | Fragment | Owner |
|---|---|---|
| CEG-W (A3, A3b, A6) | `ceg-w.mk` | campaign EIE-CEG-GATE-COLLAB-2026-11-21 |
