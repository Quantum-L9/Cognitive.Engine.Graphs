# Progress this sprint

- 2026-07-24 — **Suite-6 full cut-over planned** (no edits; plan at
  `~/.cursor/plans/port_profiles_into_components_12de56c7.plan.md`). Scope grew from "port the 12
  `profiles/` docs" to a 9-phase cut-over of `$HOME/.cursor-governance` after the survey showed the
  residue is self-reinforcing: `ops/scripts/verify-setup-alignment.sh` Test 6 **fails** when the
  count of `=== SUITE 6 CANONICAL HEADER ===` files drops below 8, and
  `operational-oversight.py:354` invokes it as "the current active replacement" — so the system
  pays to keep the thing being removed. That single fact fixes phase order: enforcer first,
  headers second. 38 headers live (12 in `profiles/`, 26 in components); `L9_META` already at 87
  files, so this finishes a migration ~70% complete rather than starting one.
  Three draft claims were overturned by verification: sections B-E of `session-startup-protocol.md`
  are a **foreign project's** checklists (cite `SUPABASE_AUTH_CORRECT_METHOD.md`,
  `Data_Management/supabase-schema.sql`, `Configuration/.env` — all missing; 12 Supabase/n8n
  markers), so only F/G/H port; `security/credentials-policy.md` is a **stale path** not a missing
  file (`learning/credentials-policy.md` exists) so it gets repointed not dropped; and the
  `profiles/` hits in `kernels/L9 Coding Control Plane/` are **false positives** diagramming that
  kernel's own tree. Also found `.suite6-config.json` is **manifested** (`l9-meta.yaml:489`,
  `l9_template_manifest.yaml:127`) so it scaffolds into every new L9 repo, and
  `ops/scripts/session_init.sh:83-84` logs two profile filenames that have never existed.
  Graphiti MCP tool plane unreachable — state written to `memory-bank/` only.
- 2026-07-24 — **PR #147 opened**: L9_META pipeline rebuilt config-driven, schema v2, full-repo
  stamp. `FILE_REGISTRY` deleted in favor of `l9-meta.yaml` path rules; the two stub formatters
  (comment, docstring) restored and the format layer split into `tools/l9_meta/formats/`.
  Detection is now anchored at column 0 and bounded to the leading comment region — without
  both, the tool could not stamp itself: the `--- L9_META ---` examples inside `comment.py`'s
  and `docstring.py`'s own docstrings parsed as malformed headers, and an `L9_META` literal in
  a test fixture was read as that file's header. Discovery via `git ls-files -z` (paths with
  spaces survive; generated output cannot be stamped). Markdown insertion is front-matter aware.
  Tags folded 343 → 188 across three facets with a `min_files` floor. Unknown config keys are
  rejected rather than ignored. `**/*.json` excluded (DEFERRED-003) because injection
  round-trips through the parser and reserializes whole files. 607 files, 3 commits,
  607/607 consistent, second apply writes 0, 1416 tests pass. Worktree off `origin/main`.
- 2026-07-24 — **PR #146 opened**: contract alignment, phases 1–6. `contracts/*.yaml` and
  `docs/contracts/*.md` are now one registry split by concern (YAML = identity/wiring,
  Markdown = prose), with `tests/contracts/test_contract_registry.py` as a blocking drift gate.
  `verification.test` moved from file paths to pytest node IDs; every YAML gained a `docs:`
  pointer; 7 missing docs written (C-10, 11, 15, 18, 20, 21, 22) and 10 expanded, taking the
  doc count 20 → 27. `TestContract21`–`24` closed the test gap. `STUB-001/002/003` added for
  the zero-stub protocol, scoped to `engine/` so chassis ABCs aren't false positives.
  Two doc-vs-code contradictions resolved in favor of the code: gate count is 10 (not 14), and
  both `logging.getLogger` and `structlog.get_logger` satisfy CONTRACT-04. 66 files,
  +1604/−178, 1749 tests passing. Built in a worktree off `origin/main`.
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
- 2026-07-24 — **Governance IDE profile + `make start` + bidirectional sync**
  ([Cursor-Governance#17](https://github.com/Quantum-L9/Cursor-Governance/pull/17) MERGED `2522fa4`;
  [CEG#148](https://github.com/Quantum-L9/Cognitive.Engine.Graphs/pull/148) open). Built an IDE
  profile SSOT under `environment/ide/` — Biome as the Node default, Ruff/Pyright for Python,
  ESLint kept available for named `eslint_owned` repos (Website-Bot/SEO-Bot) so Biome is never
  forced onto them. `.vscode/settings.json` is reconciled by **managed-key merge**: only declared
  keys are touched, user keys survive, and a `.l9-ide-desired-hash` stamp makes reruns idempotent.
  17-assertion fixture selftest covers classification, formatter exclusivity, key preservation,
  idempotency, and JSONC safety. The sync change is the load-bearing one: `governance_sync.sh`
  only pulled, and the push lived solely in the `sessionEnd` hook, so any crash or hook timeout
  left local governance work unpushed **indefinitely** — it now pushes after the pull by
  delegating to `backup_to_github.sh` (the same script `sessionEnd` calls, so one implementation
  and no drift), inheriting its unmerged-path/conflict-marker/stale-lock guards, with the
  single-flight lock spanning both halves. `make start` runs the real `sessionStart` hook
  synchronously via `L9_BOOTSTRAP_SYNC=1` so the manual path cannot diverge from the automatic
  one. Two failure modes stopped being silent: a failed ff-only and a failed push now print to
  stderr instead of hiding behind `|| true`. Merged rather than gated because that clone
  auto-pushes to `main` by design — holding it behind a PR would have required disabling the
  durability guarantee, and the gate was illusory anyway (no branch protection, sole reviewer,
  CI red on `main` for ≥5 runs from unrelated `WIP/Graphiti` ruff errors). Graphiti unreachable —
  state written to `memory-bank/` only.
