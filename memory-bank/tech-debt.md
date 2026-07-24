# Tech debt — revisit later

- **`constellation-node-sdk` is an unpinned git dependency** (`pyproject.toml`), so upstream
  changes to `create_node_app`, `NodeRuntimeConfig`, or `TransportPacket` can break the SDK
  chassis without a version bump. Pin to a tag or commit before the SDK path becomes default.
- **`chassis/auth/app.py` is deleted but not resolved** — quarantined rather than fixed forward.
  Decide its fate when the SDK chassis takes over auth.
- **Health probes assert HTTP 200 only** (3 Dockerfiles + `docker-compose.yml:71`). Correct for
  the legacy chassis (returns 503 when unready) but silently false-healthy under the SDK, whose
  `/v1/health` always returns 200 with a `ready` boolean. Tracked as step 6 of the SDK plan.
- **Graphiti memory was unreachable on 2026-07-24** — session state went to `memory-bank/` only,
  so nothing from these sessions is in the episodic graph. Four consecutive sessions now missing.
- **`mypy` on PATH is Python 3.9 (Xcode), project targets 3.12** — `make agent-check` and
  `make lint` fail locally at the type-check step with a bogus "Invalid syntax" on
  `engine/inference_rule_registry.py:462`. `mypy` is not installed in the project venv. CI is
  unaffected. Install mypy into the venv so the local gate matches CI.
- **`jsonschema` and `openapi-spec-validator` are undeclared dependencies** — three contract
  tests `skipif` on the import, so they silently pass as skips in a clean environment while
  appearing to cover the OpenAPI/JSON-Schema surface. Documented in `docs/contracts/README.md`
  known-gaps table; the real fix is declaring them in `pyproject.toml`.
- ~~**Contract count is inconsistent across sources**~~ — RESOLVED in PR #146. 24 invariants
  (`contracts/*.yaml`), 27 prose docs (`docs/contracts/`); the split is documented in
  `contracts/README.md` and enforced by `tests/contracts/test_contract_registry.py`.
  One straggler remains: `docs/contracts/README.md` still says 20 (see `tasks.md`).
- **The main working tree carries ≥3 interleaved uncommitted workstreams** (now 633 changed
  files). Both PR #144 and PR #146 had to be assembled in separate worktrees to isolate a
  single stream, and PR #146 had to exclude two files whose hunks were entangled with unrelated
  work. This is now actively costing scope. Land or shelve the others.
- **An L9_META v2 injector run rewrote metadata headers across many tracked files** mid-session,
  interleaving with hand edits. Splitting the two required a throwaway hunk-classifier script.
  Run the injector on a clean tree, as its own commit. (PR #147 does exactly this, and the
  `l9-meta-check` hook makes drift visible at commit time going forward.)
- **JSON files carry no L9_META header** (16 of them) — `**/*.json` is excluded because injection
  round-trips through `json.loads`/`dumps`, which reserializes the whole file: blank lines vanish
  and compact arrays explode one element per line. `packet-envelope.schema.json` came back
  +159/−29 for a 9-line header. Tracked as DEFERRED-003; fix is a textual insert.
- **`Cursor-Governance` CI is red on `main`, continuously** — the last ≥5 runs of "L9 Lint and
  Test" all fail, every error from ruff on `WIP/Graphiti - Cirsor Governance/L9-Graphite-Memory 4/`
  (UP045, F401, I001, E501, F841). CI therefore cannot tell you whether a governance change is
  healthy, and PR checks inherit a red baseline. Either fix that WIP tree or exclude it from the
  ruff target.
- **`$HOME/.cursor-governance` `main` has no branch protection** — confirmed via the API (HTTP 404
  "Branch not protected"). Combined with a clone that auto-commits and auto-pushes to `main`, a PR
  on that repo is a record, not a gate. Fine as a deliberate choice; a hazard if anyone assumes
  review is enforced.
- **The governance clone accepts concurrent writers with no coordination** — during the
  2026-07-24T22:32Z session another window or background agent added `ops/scripts/backup_gate.sh`,
  `ops/scripts/test_backup_gate.sh`, and ~65 lines to `ops/hooks/session_end_governance_backup.sh`
  while a fast-forward was in progress. The single-flight lock guards sync against sync, not sync
  against a human or agent editing files. Auto-commit will sweep whatever is present.
- **`mypy` reports a syntax error on `engine/inference_rule_registry.py` from `origin/main`** —
  reproduced on a clean worktree, so it predates the current workstreams and is not caused by
  header injection. Likely the same Python 3.9-vs-3.12 `mypy` mismatch noted above, but it was
  not confirmed. Confirm which before treating it as environmental.
