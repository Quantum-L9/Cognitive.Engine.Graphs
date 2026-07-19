<!-- L9_META
l9_schema: 1
origin: audit-corrected
engine: graph
layer: [ci]
tags: [ci, pipeline, pre-commit, enforcement]
owner: platform
status: active
/L9_META -->

# CI_PIPELINE.md — CI Pipeline & Pre-Commit Hooks

**Purpose**: Documents CI pipeline phases, pre-commit hooks, and blocking/advisory decision model.

**Source**: .github/workflows/ci.yml, .github/workflows/ci-quality.yml, .github/workflows/lint-autofix.yml, .pre-commit-config.yaml, pytest.ini

**Provenance**: Extracted from external audit documentation pack (2026-04-02). Verified against live repo 2026-04-26. Updated 2026-07-19 (pre-commit CI enforcement, mypy scope/blocking fix, quality-gate hyphen-notation bug, pytest fail-fast removal).

---

## CI Pipeline (8 Phases)

### Phase 1: Validation
- Check Python syntax (all .py files)
- Validate YAML files (.github/workflows/*.yml)

### Phase 2: Lint & Type Check
- Ruff linter (`ruff check`)
- Ruff formatter (`ruff format --check`)
- Mypy type checker, scoped to `engine/` (BLOCKING — see note below)

### Phase 2.5: Pre-commit Hooks
- `pre-commit run --all-files` — runs every hook in `.pre-commit-config.yaml`
  in CI, so hooks are enforced even if a contributor (human or agent) never
  ran `pre-commit install` locally, or bypassed hooks with `git commit -n`
- `SKIP=gitleaks,packet-envelope-prohibited` (see "Pre-Commit Hooks" below
  for why those two are skipped here specifically)
- `pre-commit run --all-files` does not stop at the first failing hook —
  every hook's result is reported in one run

### Phase 3: Test Suite
- Pytest with coverage (`pytest --cov`)
- Minimum coverage: 60% (CI), 70% (pyproject.toml), 95% (engine/gates/, engine/scoring/)
- `pytest.ini` has no `-x` (fail-fast): a single run reports every failing
  test instead of stopping at the first one, while still exiting non-zero
  (failing the job) if any test fails
- PostgreSQL service (postgres:16)
- Redis service (redis:7-alpine)

### Phase 4: Security Scanning
- Gitleaks (secret detection)
- pip-audit (dependency vulnerabilities, non-blocking)
- Safety (vulnerability scanner, non-blocking)
- Bandit (SAST, non-blocking)

### Phase 5: SBOM Generation
- Anchore SBOM action (spdx-json format)

### Phase 6: OpenSSF Scorecard
- Security posture scoring

### Phase 7: CI Gate (Fan-In)
- **Blocking**: validate, lint, precommit, test (must pass)
- **Advisory**: security, sbom, scorecard (failures logged, not blocking)

> `ci-quality.yml` (a separate workflow, not part of `ci.yml`'s fan-in) has
> its own `quality-gate` job that similarly aggregates `lint-format`,
> `semgrep`, `secrets-scan`, and `coverage`. Both `ci-gate` and
> `quality-gate` should be configured as **required status checks** in
> branch protection for either to actually block a merge — a workflow job
> failing does not, by itself, prevent merging unless GitHub is configured
> to require it.

---

## Decision Model

```
Phase 1: Validation      → BLOCKING
Phase 2: Lint & Type     → BLOCKING (mypy included — see note below)
Phase 2.5: Pre-commit    → BLOCKING
Phase 3: Test Suite      → BLOCKING
Phase 4: Security        → ADVISORY (non-blocking)
Phase 5: SBOM            → ADVISORY
Phase 6: Scorecard       → ADVISORY
Phase 7: CI Gate (Fan-In)→ BLOCKING (checks Phases 1-3 + 2.5)
```

**Merge-Blocking Gates**: validate, lint, precommit, test (4 jobs)
**Non-Blocking Jobs**: security, sbom, scorecard (3 jobs)

**Mypy is now blocking in `ci.yml`.** It previously ran `mypy .`
(whole-repo) piped through `|| echo "non-blocking"`, which silenced every
result — and `mypy .` itself failed immediately with `Source file found
twice under different module names` (`tools/auditors/base.py` vs
`auditors.base`) before checking a single file, so this step was
effectively a no-op. It now runs the same scoped, working invocation as
`make lint` / `ci-quality.yml` (`mypy engine/ --config-file=pyproject.toml
--ignore-missing-imports --exclude chassis`) with no output-silencing, so
real type errors fail the job.

---

## Pre-Commit Hooks

See `.pre-commit-config.yaml` for the authoritative, current hook list —
ruff/ruff-format, gitleaks, mypy, `pytest-unit`, `block-fastapi-in-engine`,
`check-cypher-interpolation`, `l9-contract-scan`, `l9-contract-files-exist`,
`l9-audit-harness` (pre-push stage), `fix-deprecated-imports` /
`check-deprecated-imports`, `packet-envelope-prohibited`,
`terminology-guard`, and the standard `pre-commit-hooks` set
(`check-yaml`, `end-of-file-fixer`, `trailing-whitespace`,
`check-added-large-files`).

**Locally**: install once with `pre-commit install`; hooks then run on every
`git commit`.

**In CI**: the `precommit` job in `ci.yml` (Phase 2.5) runs
`pre-commit run --all-files` on every PR/push via `pre-commit/action`, so
these hooks are enforced regardless of whether a contributor has
`pre-commit install`ed locally. Two hooks are skipped there with
`SKIP=gitleaks,packet-envelope-prohibited`:

- `gitleaks` — the hook runs `gitleaks protect --staged`, which inspects
  the git *index*. That has no equivalent in a plain CI checkout (nothing
  is "staged"). Secret scanning is already covered correctly by the
  `security` job in `ci.yml`, which uses `gitleaks/gitleaks-action` to scan
  the actual commit range.
- `packet-envelope-prohibited` — as of this writing there are 23 pre-existing
  references to the deprecated `PacketEnvelope` model across `engine/` and
  `chassis/` (superseded by `TransportPacket` from `constellation_node_sdk`,
  see `docs/contracts/SHARED_MODELS.md`). That's a real, separately-tracked
  architecture migration, not a CI-configuration problem — skipped here so
  this job reflects the currently-addressable baseline rather than being
  permanently red on a known, unrelated migration debt. Run
  `python tools/check_packet_envelope_prohibited.py --check .` locally to
  see the current list; remove the skip once that migration lands.

**Pre-existing pytest-unit exclusions** (`--ignore=...` in the hook's
`entry:`) — `test_gates_all_types.py`, `test_scoring.py`, `test_config.py`,
`test_arbitration.py`, `test_wave6_dormant_features.py` — require mock
updates and must be removed once those are completed.

---

## Agent Decision Matrix for CI Failures

| CI Step Fails | Action |
|---------------|--------|
| validate (syntax/YAML) | STOP — fix syntax errors |
| lint (ruff check) | STOP — run `make lint-fix` |
| lint (ruff format) | STOP — run `ruff format .` |
| lint (mypy) | STOP — fix the reported type error(s); mypy is blocking |
| precommit (any hook) | STOP — run `pre-commit run --all-files` locally, fix, re-commit |
| test (pytest) | STOP — fix failing tests or add coverage (full failure list is reported, not just the first) |
| security (gitleaks) | STOP — remove secret, add to vault |
| security (pip-audit) | WARN — triage vulnerability, create security issue |
| security (safety) | WARN — review warning, create issue if legitimate |
| security (bandit) | WARN — review warning, suppress if false positive |
| sbom | WARN — investigate Anchore failure, retry |
| scorecard | WARN — investigate scorecard failure, retry |

---

## Fixed: `ci.yml` (and 4 other workflows) never actually triggered

`ci.yml`'s `on.push.branches` / `on.pull_request.branches` were set to
`${{ vars.PRIMARY_BRANCH || 'main' }}` / `${{ vars.DEVELOP_BRANCH ||
'develop' }}`. GitHub Actions does not support context expressions in
`on:` trigger filters — they're evaluated before a workflow run is even
created, so a workflow file using one there is rejected outright ("This
run likely failed because of a workflow file issue", 0 jobs created),
regardless of what the rest of the file contains. `docker-build.yml`,
`supply-chain.yml`, `codeql.yml`, and `release-drafter.yml` had the exact
same bug. Confirmed via `gh run list`: every push-triggered run of these
5 files failed at 0s, and none of them ever appear as a successful
pull_request-triggered run either — meaning `ci.yml` (the main "CI
Pipeline" workflow) has never actually executed in this repository, on
any event. Fixed by hardcoding the branch names the `|| 'default'`
fallback already implied.

---

## Fixed: `ci-quality.yml` quality-gate fan-in bug

`quality-gate`'s summary/fail logic referenced `needs.lint-format.result`,
`needs.secrets-scan.result`, and `needs.yaml-validate.result` using GitHub
Actions dot notation. Dot notation does not work for job IDs containing a
hyphen (`-` is parsed as subtraction), so those three always evaluated to
an empty string — the corresponding `if` conditions could never actually
be true, meaning `quality-gate` could report ✅ even when `lint-format` or
`secrets-scan` failed. `semgrep`'s result also wasn't checked at all.
Fixed by switching to bracket syntax (`needs['lint-format'].result`, etc.)
and adding the missing `semgrep` failure check.

---

## Coverage Threshold Hierarchy

| Source | Threshold | Scope |
|--------|-----------|-------|
| ci.yml `COVERAGE_THRESHOLD` | 60% | CI default |
| pyproject.toml `fail_under` | 70% | Global minimum |
| TESTING.md layer-specific | 95% | engine/gates/, engine/scoring/ |

**Hierarchy**: Layer-specific (95%) > pyproject.toml global (70%) > CI default (60%)

---

## Related Documents

- **TESTING.md** — Test structure and coverage thresholds
- **GUARDRAILS.md** — Banned patterns registry
- **docs/TROUBLESHOOTING.md** — Common CI failure resolutions
- **.github/workflows/ci.yml** — Pipeline definition (source of truth)
- **.pre-commit-config.yaml** — Hook configuration (source of truth)
