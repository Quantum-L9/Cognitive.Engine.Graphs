<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [audit]
tags: [auditors, remediation, logging]
owner: engine-team
status: active
/L9_META -->

# Remediation: `log_safety` auditor

Fix procedure for findings emitted by `tools/auditors/log_safety.py`.
Reproduce with: `python tools/audit_dispatch.py --auditor log_safety`

```
task: Fix log safety finding "<finding_code>"
tier: 1
contracts_to_read:
  - docs/contracts/ERROR_HANDLING.md
  - docs/contracts/OBSERVABILITY.md
```

## Steps

### SENSITIVE_LOGGED / CREDENTIAL_PRINT
- Remove the sensitive variable from log/print
- If debugging is needed, log a masked version: `***{last4}`
- Never log: password, token, secret, api_key, ssn, credit_card

### STACK_TRACE_LEAK
- Replace `return {"error": str(e)}` with generic message
- Log the full exception internally with `logger.exception("...")`

## Acceptance
- [ ] Finding gone from audit output
- [ ] No sensitive tokens in any log/print statement
- [ ] `make agent-check` exits 0
