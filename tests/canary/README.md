<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [ci, canary, semgrep, fixtures]
owner: platform
status: active
/L9_META -->

# CI canary fixtures — DO NOT MERGE

Disposable fixtures for campaign `L9-CI-UNIFIED-LEVERAGE-2026-11-20-v1.7.1`,
lane CANARY-CEG. They exist only on branch `canary/ci-ul-ceg` and its pull
request, which is never merged.

Nothing here is imported or collected: no file is named `test_*.py`, and no
package `__init__.py` exists. They are inert source read by static scanners.

| File | Purpose |
|---|---|
| `central_rule_witness.py` | known-bad and known-good for the central SDK rule `l9.baseline.python.tls-verify-disabled` |
| `ceg_rule_witness.py` | known-bad and known-good for the CEG-local rule `no-structlog-getchild` |
| `override_witness.py` | a consumer `# nosemgrep` attempt to waive the central rule above |

`.semgrep/fixtures/semgrep-rules.py` also gains a `no-structlog-getchild`
pair, run by CEG's own `semgrep --test` step.
