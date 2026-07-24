# Progress this sprint

- 2026-07-24 — **PR #144 opened**: agent docs wired into the audit harness and spec coverage.
  Four auditors' remediation/contract doc paths were broken (findings linked nowhere) — fixed and
  the runbooks moved to `tools/auditors/remediation/`. `docs/Dev-Docs/` → `tools/research/`, now
  consumed by `spec_extract.py` as a `research_pattern` coverage category. Four stale dev playbooks
  consolidated into `.claude/skills/` and deprecated (DEFERRED-004 tracks deletion). Added
  `make agent-check` and corrected `docs/contracts/README.md`. 35 files, 2 new test modules
  (37 tests). Built in an isolated worktree off `origin/main` to keep it clean of the three
  unrelated workstreams sitting in the main working tree.
- 2026-07-24 — Gate_SDK chassis migration plan authored and hardened via the `Improve.md` kernel
  (discovery pass against repo + SDK source). Two open decisions resolved: `ACTION_HANDLERS` as
  single source of truth, and `org_id == domain_id` tenant passthrough. Plan is locked at 8 steps;
  **no implementation started**.
