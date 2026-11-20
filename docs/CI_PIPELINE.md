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
- `sdk-channel` (networked, gated by `ci-gate`): `scripts/validate_sdk_pin.py --verify-tag`, then
  `uv lock --check`, which resolves `pyproject.toml` with `uv.lock` as the preference and fails
  when a dependency changed without `uv lock` (or a pinned release was yanked). It never moves a
  locked version; `uv lock --upgrade-package <name>` (or Dependabot's uv PR) does.

### Phase 2: Lint & Type Check
- Ruff linter (`ruff check`)
- Ruff formatter (`ruff format --check`)
- Mypy type checker, scoped to `engine/` (BLOCKING — see note below)

### Phase 2.5: Pre-commit Hooks
- `pre-commit run --all-files` — runs every hook in `.pre-commit-config.yaml`
  in CI, so hooks are enforced even if a contributor (human or agent) never
  ran `pre-commit install` locally, or bypassed hooks with `git commit -n`
- `SKIP=gitleaks` (see "Pre-Commit Hooks" below for why gitleaks is skipped
  here specifically; `packet-envelope-prohibited` is deliberately NOT
  skipped — see below)
- `pre-commit run --all-files` does not stop at the first failing hook —
  every hook's result is reported in one run

### Phase 3: Test Suite
- Pytest with coverage (`pytest --cov`)
- Minimum coverage: 60% (CI), 70% (pyproject.toml), 95% (engine/gates/, engine/scoring/)
- `pytest.ini` has no `-x` (fail-fast): a single run reports every failing
  test instead of stopping at the first one, while still exiting non-zero
  (failing the job) if any test fails
- PostgreSQL service (postgres:16)

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
> its own `quality-gate` job that similarly aggregates `semgrep`,
> `secrets-scan`, and `yaml-validate` (`lint-format` moved to its one owner,
> see "One owner per check"). Both `ci-gate` and
> `quality-gate` should be configured as **required status checks** in
> branch protection for either to actually block a merge — a workflow job
> failing does not, by itself, prevent merging unless GitHub is configured
> to require it.

> `ci-quality.yml`'s `semgrep` job runs the policy scan (`semgrep ci` with
> `.semgrep/` rules, inside `returntocorp/semgrep-agent:v1`, semgrep 1.36.0),
> then `semgrep --test` on `.semgrep/fixtures/semgrep-rules.py` in the same
> image. The fixtures pin each rule's true positives and true negatives, so a
> rule must be precise rather than silenced line by line with `nosemgrep`.
> `.semgrepignore` restates semgrep's default ignore list and adds the
> fixture directory.

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
`pre-commit install`ed locally. One hook is skipped there with
`SKIP=gitleaks`:

- `gitleaks` — the hook runs `gitleaks protect --staged`, which inspects
  the git *index*. That has no equivalent in a plain CI checkout (nothing
  is "staged"). Secret scanning is already covered correctly by the
  `security` job in `ci.yml`, which uses `gitleaks/gitleaks-action` to scan
  the actual commit range.

`packet-envelope-prohibited` is deliberately **not** skipped — and it is
**ledger-governed**. The hook (`tools/packet_envelope_gate.py`)
provisions the pinned `l9-ci-sdk` revision and runs its deterministic
full-repository AST scanner, then compares every finding fingerprint
against the human-reviewed debt ledger at
`.l9/baselines/packet-envelope.yml`. Pre-existing references to the
deprecated `PacketEnvelope` model (superseded by `TransportPacket`, see
`docs/contracts/SHARED_MODELS.md`) are recorded there — each with an
owner, a tracking issue, an expiry date, and the machine-evaluable removal
condition "migrated to TransportPacket". Known recorded debt is tolerated
until expiry; any **new, increased, moved, expired, or unowned** reference
blocks the commit. CI runs the same hook in `ci.yml`'s `precommit` job,
so local hooks and CI can never disagree. (The `l9-ci-core` Baseline Ratchet
caller that also ran it was retired in #335: Core no longer ships its
reusable workflow.) The ledger is CODEOWNERS-protected and is **never** written by
CI — only humans change it, in reviewed PRs. The legacy grep-based
`tools/check_packet_envelope_prohibited.py` was deleted (superseded by the
SDK scanner). Run `pre-commit run packet-envelope-prohibited --all-files`
locally to see the current verdict.

**Pre-existing pytest-unit exclusions** (`--ignore=...` in the hook's
`entry:`) — `test_gates_all_types.py`, `test_scoring.py`, `test_config.py`,
`test_arbitration.py`, `test_wave6_dormant_features.py` — require mock
updates and must be removed once those are completed.

---

## Autofix

Two ways to autofix lint violations that `ruff check`/`ruff format` can
mechanically resolve (unsorted imports, unused imports, formatting,
`zip(strict=True)`, `Optional[T]` -> `T | None`, etc.):

1. **Locally**: run `make lint-fix` (`ruff check . --fix && ruff format .`),
   then re-run `make lint` to confirm the fix and see anything left that
   needs a manual change (e.g. `mypy` errors, or ruff rules with no safe
   fixer such as `PLR0915`).
2. **In CI**: `.github/workflows/lint-autofix.yml` runs the same two
   commands against `develop` (on push or via `workflow_dispatch`) and
   opens a PR with the results — it never pushes directly to a protected
   branch. This replaced the former `auto-fix-adr.yml`, which depended on
   `ci/auto_fix_adr.py` and `ci/check_imports.py`; neither script existed
   in this repo, so that workflow could never succeed.

Non-autofixable violations (most `mypy` errors, `PLR0915`, etc.) require
a manual fix — `make lint` fails with the specific rule/line to address.

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

## Native outcomes: a job conclusion is not a scan result

Three controls call an outside service. Each one reports what the service
actually did, through `tools/scanner_outcomes.py`, in the step summary and as a
job output. The witnesses are `tests/unit/test_scanner_outcomes.py` and the
fixture `tests/fixtures/ci/quality_gate_cases.yaml`.

**GitGuardian (`ci-quality.yml`, `secrets-scan`).** Output `native_outcome`:

| Outcome | Scan executed | Job | Counted by `quality-gate` |
| --- | --- | --- | --- |
| `SCANNED_CLEAN` | yes | passes | executed |
| `SECRET_DETECTED` | yes | fails | failed |
| `SCANNER_FAILED` (the container started and ggshield failed, including a missing CI context) | no | fails | failed |
| `KEY_ABSENT` | no | passes (advisory skip) | not executed |
| `TOKEN_LACKS_SCOPE` | no | passes (advisory skip) | not executed |
| `CONTAINER_FAILED_TO_START` | no | passes (advisory skip) | not executed |

Which outcomes fail the job is the policy the workflow already had. The
container is given the runner's `CI` and `GITHUB_ACTIONS` variables, which
ggshield requires, and a container failure is read from the `docker run` exit
code, never from the text `Unable to find image` that precedes every cold pull.

`quality-gate` fails when any job does not conclude `success`, as before. An
advisory skip is listed as `NOT EXECUTED` and is not counted, and `success`
without a recognised native outcome fails. Its headline reads "N of 4 controls
executed".

**Dependency review (`supply-chain.yml`, `dependency-review`).** Outputs `graph`
and `review` (`EXECUTED_PASSED`, `EXECUTED_FAILED`, `NOT_EXECUTED`). `graph` is
`AVAILABLE` when the compare API answers, `UNAVAILABLE` on HTTP 404 or 403 (not
enabled or not readable), and `UNKNOWN` when the check itself fails, for
example on a 502. A graph that is not `AVAILABLE` still does not fail the job;
the summary states that no dependency was reviewed.

**Codecov (retired, T42).** No workflow uploads to Codecov. The last upload,
in `dev-layer-gmp.yml`, last ran on 2026-03-11 and failed on every run; it is
removed. `dev-layer-gmp.yml` keeps its `--cov-fail-under=80` threshold. The
deep plan keeps `coverage.xml` as an artifact. Coverage reporting moves to
SonarQube Cloud, which this change does not wire.

---

## One owner per check (consolidation)

Each local check runs in one workflow. On every pull request the required
Analyze (central Core) job runs the same `Repo.mk` leaves again (`make
validate`, `make check`), with tools installed from Core's v2 pins by the same
`tools/deps/install_locked.sh` the local jobs use. Central Core does not run on
push to `main`, so the local owner is what checks `main`.

| Check | Local owner (PR + push to main) | Central (PR) | Removed copies |
|---|---|---|---|
| `ruff check .` | `l9-lint-test.yml` (org-distributed) | `make check` | `ci.yml` lint, `ci-quality.yml` lint-format, `contracts.yml` lint, `compliance.yml`, pre-commit `ruff` in CI |
| `ruff format --check .` | `l9-lint-test.yml` | `make check` | `ci.yml` lint, `ci-quality.yml` lint-format, `contracts.yml` lint, pre-commit `ruff-format` in CI |
| `mypy engine/` | `l9-lint-test.yml` | `make check` | `ci.yml` lint, `ci-quality.yml` lint-format, `contracts.yml` lint, `compliance.yml`, pre-commit `mypy` in CI |
| `tools/contract_scanner.py` | `contracts.yml` | `make validate` | `compliance.yml`, pre-commit `l9-contract-scan` in CI |
| `tools/verify_contracts.py` | `contracts.yml` | `make validate` | pre-commit `l9-contract-files-exist` in CI |
| `tools/l9_meta_injector.py` | `contracts.yml` | `make validate` | pre-commit `l9-meta-check` in CI |
| `tools/check_action_refs.py` | `contracts.yml` | `make validate` | none |

Local pre-commit still runs every hook; only the CI pre-commit pass skips the
copies above. `lint-autofix.yml` (rewrites `main`, opens a fix PR) and
`refactoring-validation.yml` (`refactor/*` only) are not check owners.
`tests/unit/test_workflow_triggers.py` fails on a second owner.

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
- **docs/CI_CONSTELLATION_BOUNDARY.md** — What's wired from the Quantum-L9
  constellation (`l9-ci-core` `Analyze (central Core)`, `l9-ci-sdk` packet-envelope
  scanner) vs. not (`l9-harness`, `l9-assurance`); read before extending
  that integration or touching `audit.yml`
- **.github/workflows/ci.yml** — Pipeline definition (source of truth)
- **.pre-commit-config.yaml** — Hook configuration (source of truth)
