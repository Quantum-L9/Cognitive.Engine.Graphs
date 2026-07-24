<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [audit]
tags: [auditors, remediation, api]
owner: engine-team
status: active
/L9_META -->

# Remediation: `api_regression` auditor

Fix procedure for findings emitted by `tools/auditors/api_regression.py`.
Reproduce with: `python tools/audit_dispatch.py --auditor api_regression`

```
task: Fix API regression "<finding_code>"
tier: 2
contracts_to_read:
  - docs/contracts/METHOD_SIGNATURES.md
  - docs/contracts/RETURN_VALUES.md
```

## Steps
1. Read the finding: what changed (class removed, method removed, signature changed)
2. **CLASS_REMOVED:** restore class or add deprecation shim
3. **METHOD_REMOVED:** restore with deprecation warning + alias
4. **SIGNATURE_CHANGED:**
   a. Update `docs/contracts/METHOD_SIGNATURES.md` with new signature
   b. `grep -rn "ClassName(" engine/ tests/` — find all callers
   c. Update every caller to match
   d. Re-run auditor — finding disappears
5. Run `make agent-check`

## Acceptance
- [ ] Finding gone from audit output
- [ ] METHOD_SIGNATURES.md updated
- [ ] All callers updated
- [ ] Tests pass
- [ ] `make agent-check` exits 0
