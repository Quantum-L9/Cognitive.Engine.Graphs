# Where we left off (max ~1 screen)

**Last session:** 2026-07-24T22:50Z
**Repo:** /Users/ib-mac/Dropbox/Repo_Dropbox_IB/Cognitive.Engine.Graphs
**Branch (main worktree):** `fix/docker-git-deps-and-proprietary-license` — dirty, ~620 modified files

## Session summary

Rebuilt the L9_META injection pipeline and shipped it as **PR #147**, then untangled the
local git state so every stream has a home.

The old injector kept a hardcoded `FILE_REGISTRY` as its source of truth and two of its five
formatters were stubs that emitted a delimiter pair with no field lines — so `--apply` on any
YAML, shell, Makefile, or Python path wrote an empty block. The registry had drifted to 10
ghost entries while ~500 tracked files were invisible to it. Replaced with `l9-meta.yaml`
(`defaults -> rules (last match wins) -> overrides[exact path]`), formatters restored and split
into `tools/l9_meta/formats/`, discovery via `git ls-files -z`, tags folded 343 -> 188 across
three facets, schema v2 drops `owner`. Enforced by an `l9-meta-check` pre-commit hook and a
`meta-headers` CI job.

## PR map — five open streams from this repo

| PR | Branch | Contains |
|---|---|---|
| #148 | `feat/make-start-governance` | `make start` target + `docs/CI_CONSTELLATION_BOUNDARY.md` |
| #147 | `feat/l9-meta-pipeline` | L9_META pipeline + the full-repo header stamp (607 files) |
| #146 | `docs/contract-alignment` | 24 YAML invariants ↔ 27 prose docs reconciled |
| #145 | `chore/track-memory-bank` | this directory, tracked as T0 resume SSOT |
| #144 | `docs/wire-agent-docs-into-tooling` | auditor remediation paths + research pattern coverage |

**PR #147 supersedes the local header edits.** 419 of the 439 header-only modified files in the
main working tree are byte-identical to #147; the other 20 differ only because #147 adds the
blank line after an injected docstring that `ruff format` requires. Do not open a second
"meta-only" PR — it would duplicate #147 and conflict with it.

## Next action

1. Review / merge in dependency order — #147 touches every file, so land it **last** or expect
   to rebase the others. #144, #145, #146, #148 are independent of each other.
2. Sync the `**/*.json` exclusion into the main tree's `l9-meta.yaml` before running
   `l9-meta apply` there. Without it, 16 JSON files get reserialized (DEFERRED-003).
3. Decide DEFERRED-004: approve deleting the four deprecated `docs/agent-tasks/` playbooks.
4. `docs/Commands.md` is a 0-byte stray in the main tree — write it or delete it.
5. Resume the Gate_SDK chassis plan (8 locked steps in `tasks.md`) — no implementation started.
6. Remove the temp worktrees once their branches merge: `/tmp/ceg-meta-pr`, `/tmp/ceg-pr`,
   `/tmp/ceg-mb`, `/tmp/ceg-pristine`.

## Blockers

- **Graphiti unreachable** — four consecutive sessions absent from the episodic graph.
- **`mypy` on PATH is Python 3.9 (Xcode)**, project targets 3.12 — `make lint` fails locally at
  type-check with a bogus syntax error on `engine/inference_rule_registry.py`. CI is unaffected.
- **Governance GitHub backup not run** — requires explicit push approval.
