# Queued work

## Immediate — PR #144 follow-ups (2026-07-24)

- [ ] Review / merge **PR #144** — agent docs wired into audit harness + spec coverage.
- [ ] **DEFERRED-004 decision**: approve deleting the four deprecated `docs/agent-tasks/`
      playbooks (`add-action-handler`, `add-domain-spec`, `add-gate-type`, `extend-contract`).
      They are marked deprecated and referenced by nothing; two carry actively wrong guidance.
- [ ] Remove the temp worktree: `git worktree remove /tmp/ceg-pr-wiring` (branch is pushed).
- [ ] Declare `jsonschema` and `openapi-spec-validator` in `pyproject.toml` — three contract
      tests silently skip without them and pass only because they happen to be installed locally.

## Gate_SDK chassis instantiation (locked plan, 2026-07-24)

Plan file: `~/.cursor/plans/gate_sdk_chassis_instantiation_381cad37.plan.md` (outside repo).
Execute in order — later steps depend on earlier ones.

- [ ] 1. **action-map** — add public `ACTION_HANDLERS` dict to `engine/handlers.py` as the single
      source of truth for the 8 actions; refactor `register_all` and `chassis/actions.py` to consume it.
- [ ] 2. **handler-registration** — new `chassis/handler_registration.py`: register `ACTION_HANDLERS`
      via SDK `register_handler`, wrapped with `PacketEnvelope` + `PacketStore.persist` audit,
      preserving the 2-arg `(tenant, payload)` signature.
- [ ] 3. **node-app** — new `chassis/node_app.py`: typed SDK `LifecycleHook` adapter around
      `GraphLifecycle` + `create_node_app(auto_register_with_gate=False)` (GraphLifecycle already
      calls `register_node_with_gate()`; avoid double registration).
- [ ] 4. **entrypoint** — new `chassis/entrypoint.py` dispatching on `L9_CHASSIS`; retarget all four
      launch sites (`scripts/entrypoint.sh`, `Dockerfile.prod`, `Makefile`) and add `local-api-sdk`.
- [ ] 5. **config** — gate-only + signing `NodeRuntimeConfig` env vars in `.env.template` and
      `docker-compose.yml`, incl. `L9_ENABLE_RELAY_ROUTE=false`, `HOST=0.0.0.0`,
      `L9_EXECUTE_ALLOWED_ACTIONS`.
- [ ] 6. **healthcheck** — harden all four `/v1/health` probes to assert `ready==true`
      (SDK health always returns HTTP 200).
- [ ] 7. **tests** — `tests/unit/test_node_app.py` (routing, tenant invariant, gate-only rejection,
      relay 404, failure packet, packet store, preflight) and `tests/contracts/test_chassis_parity.py`.
- [ ] 8. **housekeeping** — register new files in `tools/l9_meta_injector.py`; update
      `openapi.yaml`, `contract_02.yaml`, `DEFERRED.md` for the SDK ingress deltas.

<!-- Ported from workflow_state.md (Active TODO Plan + Next Steps) on 2026-07-24 -->
- [ ] Wire `make audit` into local/dev workflow.
- [ ] Decide whether to fail audits on MEDIUM severity.
- [ ] Decide whether to commit generated `artifacts/` outputs.
- [ ] Configure branch protection for contract-files, contract-scan, lint, test (optional).
- [ ] Run integration tests for outcome persistence with PACKET_STORE_ENABLED=true.
- [ ] Test feedback loop end-to-end: match → outcome → PacketStore linkage.
- [ ] Review remaining packs in `current work/04-25-2026/CEG/` for additional extraction candidates.

## Open Questions (ported from workflow_state.md)

- Should MEDIUM findings fail CI or remain advisory?
- Should `artifacts/` outputs be committed or ignored? Current state is split:
  `audit_report.md`, `coverage_matrix.json`, `coverage_report.md`, `spec_checklist.json`
  are tracked and were regenerated in PR #144; `harness_report.md` exists locally but is
  untracked and was deliberately left out of the PR. Pick one convention.

---
## Session 2026-07-24T19:29:26Z
No summary.

---
## Session 2026-07-24T19:31:03Z
No summary.

---
## Session 2026-07-24T19:31:42Z
No summary.

---
## Session 2026-07-24T20:01:30Z
No summary.

---
## Session 2026-07-24T20:03:03Z
No summary.

---
## Session 2026-07-24T20:23:38Z
No summary.

---
## Session 2026-07-24T20:25:45Z
No summary.

---
## Session 2026-07-24T20:26:02Z
No summary.

---
## Session 2026-07-24T21:10:10Z
Doc wiring session → PR #144. Auditor doc paths fixed, `docs/Dev-Docs/` → `tools/research/`
wired into `spec_extract.py`, `docs/agent-tasks/` split between
`tools/auditors/remediation/` and `.claude/skills/`, `make agent-check` added.
Graphiti unreachable — state written to `memory-bank/` only.
