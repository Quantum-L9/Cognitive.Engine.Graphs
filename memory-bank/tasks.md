# Queued work

## Suite-6 full cut-over (locked plan, 2026-07-24) — NEXT BUILD

Target repo: `$HOME/.cursor-governance` @ `a564f90` (NOT the CEG repo, except Phase 4).
Plan file: `~/.cursor/plans/port_profiles_into_components_12de56c7.plan.md` (filename is stale;
content is the full 9-phase cut-over).

**Phase order is load-bearing.** `verify-setup-alignment.sh` Test 6 fails when the count of
Suite-6 canonical headers drops below 8, and `operational-oversight.py:354` invokes it. Strip
headers before fixing that gate and the oversight check breaks mid-migration.

- [ ] **BLOCKER FIRST** — `$HOME/.cursor-governance` has 6 uncommitted pre-existing files, two of
      them in `ops/scripts/` which Phase 1 edits (`governance_sync.sh`, plus untracked
      `render_bootstrap_context.py`; also `session_start_bootstrap.sh` in `ops/hooks/`).
      Commit, stash, or worktree off `origin/main` before starting.
- [ ] 0. **baseline** — record git rev both repos, checksum the 38 header files.
- [ ] 1. **break-coupling** — delete Test 6 (lines 146-156) from `ops/scripts/verify-setup-alignment.sh`,
      fix its banner (line 50) and summary claims (lines 190-191), renumber remaining tests.
      Replace `"suite 6"` / `"canonical header"` in `ops/scripts/tool_pattern_extractor.py:375`
      with `"l9_meta"` / `"canonical law"`. Re-run the oversight verification path.
- [ ] 2. **port-profiles** — 12 `profiles/` files into skills `references/`, `AUTONOMY_MANIFEST.yaml`,
      and one new `rules/45-pre-action-verification.mdc` (condensed 60-80 lines from sections
      F/G/H only — drop B-E as foreign-stack). Every new reference file MUST get an entry in its
      owning `SKILL.md` Resource Map or it is never loaded.
- [ ] 3. **migrate-headers** — 26 live non-profile files from Suite-6 canonical header to `L9_META`.
      Map fields, do not transcribe: Suite-6 carries ~40 fields, `L9_META` carries 6-7.
      `learning/graphiti-episodes/quick-fixes.episodes.json` has the header inside a JSON `"body"`
      string — that is stored memory content, route to the gate, do not silently rewrite.
      Migrate `verify-setup-alignment.sh`'s own header last (after Phase 1 removes Test 6).
- [ ] 4. **remove-config** (CEG repo, separate commit) — delete `.suite6-config.json`, drop entries
      from `l9-meta.yaml:489` and `tools/l9_template_manifest.yaml:127`, check `.gitignore` and
      any template validator asserting the file exists.
- [ ] 5. **clean-wip** — 96 tracked files under `WIP/Graphiti - Cirsor Governance/`. The
      `WHOLE REPO-l9-graphiti-memory.txt` dump (10 hits) is generated — propose regeneration or
      deletion at the gate rather than hand-editing.
- [ ] 6. **fix-refs** — `ops/scripts/session_init.sh:83-84` logs `profiles/reasoning.md` and
      `profiles/technical-operations-reasoning.md`, **neither filename has ever existed**.
      Also `intelligence/adaptive-reasoning.md:66` (phantom target),
      `security/api-key-verification.md:61` + `learning/credentials-policy.md:61` (repoint),
      `key components/07_session-rebuilder.md:19` (stale `@.GlobalCommands/` prefix),
      `README.md:63`, `TODO.md:38`.
- [ ] 7. **verify** — oversight gate passes at full strength; `rg -i 'suite.?6'` outside
      `_archived/` returns hits only in the deliberate history set (`CHANGELOG.md`, `TODO.md`,
      `CANONICAL_LAW.md`, `README.md`, `SETUP_QUICK_START.md`); every new reference greps positive
      against its `SKILL.md`; governance path + symlink validators pass.
- [ ] 8. **retirement-gate** — present the deletion table (profiles ported/dropped, episodes-JSON
      decision, `WHOLE REPO-*.txt` decision, now-removable disambiguation notes in
      `environment/ide/README.md:7` and `rules/97-ide-profile-exceptions.mdc:34-36`)
      and **WAIT for explicit approval**. No deletion without it.

Preserve deliberately: Suite-6 mentions in `CHANGELOG.md`, `TODO.md`, `CANONICAL_LAW.md`,
`README.md`, `SETUP_QUICK_START.md` — they document the cut-over.

Out of scope: `kernels/L9 Coding Control Plane/` (separate architecture).

## Immediate — PR #147 follow-ups (2026-07-24)

- [ ] Review / merge **PR #147** — config-driven L9_META pipeline, schema v2, full-repo stamp.
- [ ] Sync the JSON exclusion into the main working tree's `l9-meta.yaml` before running
      `l9-meta apply` there. Without it, 16 JSON files get reserialized.
- [ ] **DEFERRED-003**: make JSON injection surgical (insert `_l9_meta` as text, leave the rest
      byte-identical), then drop `**/*.json` from `exclude` and stamp the 16 files.
- [ ] Remove the temp worktree: `git worktree remove /tmp/ceg-meta-pr` (branch is pushed).
- [ ] Decide on the three stacked worktree branches — #144, #146, #147 all branch from
      `origin/main` independently and all touch `Makefile` / docs. Merge order matters.

## Immediate — PR #146 follow-ups (2026-07-24)

- [ ] Review / merge **PR #146** — contract registry alignment (YAML ↔ docs + drift gate).
- [ ] Wire `tools/contract_report.py` into `make agent-check` as a final step. Excluded from
      #146 because the `Makefile` had unrelated uncommitted work in the same hunks.
- [ ] Fix stale doc count in `docs/contracts/README.md` — says 20, there are now 27. Same
      exclusion reason as above.
- [ ] Remove the temp worktree: `git worktree remove /tmp/ceg-pr` (branch is pushed).
- [ ] **Chassis gap, not engine:** no `structlog.configure()` exists anywhere in `chassis/`,
      so structlog currently emits through stdlib defaults. Documented in `OBSERVABILITY.md`.
      Decide whether the chassis should configure it.

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

---
## Session 2026-07-24T21:18:23Z
No summary.

---
## Session 2026-07-24T21:55:40Z
No summary.

---
## Session 2026-07-24T21:57Z
Contract alignment phases 1–6 → PR #146. YAML ↔ docs registry unified, drift gate added,
7 docs written, gate count corrected to 10, logger API contradiction resolved.
Graphiti unreachable — state written to `memory-bank/` only.

---
## Session 2026-07-24T22:01:44Z
No summary.

---
## Session 2026-07-24T22:07:01Z
No summary.

---
## Session 2026-07-24T22:09Z
L9_META pipeline rebuilt config-driven → PR #147. `FILE_REGISTRY` replaced by `l9-meta.yaml`,
stub formatters restored, tags folded 343 → 188 across three facets, schema v2, 607 files
stamped, pre-commit hook + CI job added. JSON excluded (DEFERRED-003).
Graphiti unreachable — state written to `memory-bank/` only.

---
## Session 2026-07-24T22:32Z
Governance IDE profile + `make start` + bidirectional session sync.
Cursor-Governance#17 MERGED (`2522fa4`), local clone fast-forwarded; CEG#148 open (7-line
delegating `make start` target, Makefile only). Session-start sync now pushes as well as pulls.
Graphiti unreachable — state written to `memory-bank/` only.

### Open after this session
- [ ] Merge [CEG#148](https://github.com/Quantum-L9/Cognitive.Engine.Graphs/pull/148) — 7 lines,
      Makefile only, no CI risk.
- [ ] Drop `stash@{0}` in `$HOME/.cursor-governance` — verified byte-identical to `origin/main`
      before the fast-forward, kept only as a rollback net.
- [ ] Identify the concurrent writer in `$HOME/.cursor-governance` (`ops/scripts/backup_gate.sh`,
      `ops/scripts/test_backup_gate.sh`, +65 lines in `ops/hooks/session_end_governance_backup.sh`)
      before the next sync sweeps it into a commit.
- [ ] Confirm the new push half against the real remote on a live session start — it was verified
      by syntax check and with `GOVERNANCE_SYNC_PUSH=0`, not yet on a live push.

---
## Session 2026-07-24T22:42:42Z
No summary.

---
## Session 2026-07-24T22:44:28Z
No summary.
