<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [reports]
tags: [gmp-report]
owner: engine-team
status: active
/L9_META -->

# GMP-135

**Task:** W2 CEG-side RBAC: tenant-bound capability revoke and audit
**Tier:** RUNTIME

## TODOs

- T1:  REPLACE Add capability_tenant_binding_enabled (default True) to Settings after capability_record_binding_enabled with a rationale comment; document it in a new docs/FEATURE_GATES.md section 31 (section 30 is being edited by open PR #461, so W2 must not touch it) and in .claude/rules/feature-flags.md
- T2:  REPLACE CapabilityValidator: add is_owned_by(capability_id, tenant) using check_derivation_chain (capability tenant or any ancestor tenant; '*' owns nothing) and audit_summary(tenant=None) that counts only owned capabilities and derivations when a tenant is given; ownership is read only from the registered derivation chain, so a chain broken by an unregistered parent cannot confer ownership (fail closed); in the scoped audit a derivation counts when its parent or its child capability is owned by the caller
- T3:  REPLACE handle_admin: under the flag, refuse revoke_capability for a capability the caller does not own with the unknown-id message before any mutation, and scope capability_audit and the revoke response audit to the caller
- T4:  REPLACE Unit tests for the boundary: cross-tenant revoke refused and capability unchanged; owner revoke; delegator revokes child in another tenant; '*' refused; unknown and not-owned give the same message; audit scoped; flag off restores legacy
- T5:  REPLACE SDK ingress tests: build the node app with the production L9_ALLOWED_ACTIONS parsed from docker-compose.prod.yml and prove /v1/execute refuses enrich and refuses admin for a tenant without an admin:write grant (no handler side effect); evidence: the SDK execute route validates execute_allowed_actions or allowed_actions before dispatch (constellation_node_sdk runtime/app.py:73-74,138); parse the allowlist the way tests/architecture/test_seam_gate_only.py::_allowed_actions_in does; assert a non-2xx response and that the handler was never invoked
- T6:  REPLACE Bind rbac-policy.yaml required_proof items to the new tests (verified_local) and keep external items pending on Constellation.Gate#37; switch the rbac known-gap probe to the remaining external evidence so it stays a meaningful strict xfail

## Outcome notes

- T5: the SDK answers a refused action with HTTP 200 and a `failure` packet (`return_transport_errors`), not a non-2xx status. The tests assert the failure packet and its error class: `TransportAuthorizationError` for `enrich` (allowlist, before dispatch), `ValidationError` for `admin` without `admin:write`. A positive control with the grant revokes successfully.
- Validation: make test scope 4398 passed, 6 xfailed; contracts 357 passed, 6 strict xfails (rbac now open only on Constellation.Gate#37); ruff, mypy, cypher-lint, C-18 clean.
- T1: main gained its own sections 31 and 32 while W2 was in flight, so the flag is documented in FEATURE_GATES section 33, placed after section 32 (clear of open PR #461's edits to section 30).
