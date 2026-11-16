<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, evidence]
tags: [deployment, verification, evidence, gate, eie]
owner: platform
status: active
/L9_META -->

# Pre-deployment verification — 2026-11-16

**Scope:** Gate (Quantum-L9/Constellation.Gate), CEG (this repository) and the
Gate seam with EIE (Quantum-L9/Enrichment.Inference.Engine), before the first
deployment through l9-deploy.

**Operator decision in force:** deployment is deferred until testing is
complete and every GitHub setting, key and key id is correct and confirmed
working. Nothing in this file authorizes a deployment.

**Method:** read-only. Tests ran in disposable worktrees and containers; no
repository setting, secret, environment or host was changed. Each result is
marked **PASS**, **FAIL** (a defect found), **OPEN** (a value or decision that
does not exist yet) or **UNVERIFIED** (could not be checked from this session,
with the reason).

## Revisions checked

| Repository | Ref | Commit |
|---|---|---|
| Constellation.Gate | main | `b451b68` |
| Constellation.Gate | PR #45 (l9-deploy adoption kit) | `3844db6` |
| Cognitive.Engine.Graphs | main | `aed74c9` |
| Cognitive.Engine.Graphs | PR #499 (l9-deploy adoption kit) | `8d968c4` |
| Enrichment.Inference.Engine | main | `a606791` |

## 1. Tests

| # | Check | Result | Evidence |
|---|---|---|---|
| T1 | CEG `make validate` on main | PASS | exit 0 |
| T2 | CEG `make check` (ruff, format, mypy, cypher-lint) on main | PASS | exit 0 |
| T3 | CEG unit, contract, compliance, invariant, property, architecture, security and root suites on main | PASS | 5056 passed, 11 skipped, 6 xfailed, 0 failed |
| T4 | CEG integration on main: Neo4j (testcontainers) and Postgres packet store (postgres:16-alpine, pinned digest) | PASS | 177 passed, 0 failed, 0 errors |
| T5 | Gate `make lint` and `mypy src` on main | PASS | exit 0; "no issues found in 73 source files" |
| T6 | Gate full test suite on main | PASS | 706 passed, 18 skipped |
| T7 | Gate PR #45 merged onto Gate main (merge-tree clean), full suite | PASS | 708 passed, 18 skipped |
| T8 | CI on each main | PASS (CEG, EIE); FAIL (Gate) | CEG 40 success; EIE 41 success; Gate: SonarCloud Code Analysis fails, see F6 |
| T9 | EIE test suite | not re-run here | EIE is owned by CTO EIE; its main CI is green (T8) |
| T10 | Combined Gate + CEG + EIE run (Docker rail with all three images) | **NOT RUN** | Requires building all three images and wiring signing keys that do not exist yet (K1, K2). Required before deployment is cleared. |

## 2. Gate registry parity

Gate routes an action only to a node whose registry entry lists it, and refuses
a whole registration that advertises an unknown action. PR #45 seeds Gate from
`constellation-gate/src/constellation_gate/config/node_registry.l9-runtime.yaml`.

| # | Node | Gate registry (#45) | What the node declares | Result |
|---|---|---|---|---|
| R1 | `graph` (CEG) | match, sync, outcomes, resolve, health-assess, score | `engine/spec.yaml` actions: match, sync, outcomes, resolve, health-assess, score | PASS (identical) |
| R2 | `enrichment-engine` (EIE) | converge, graph-inference-result, enrich, enrich-and-sync, enrichment-sweep, enrichment-sweep-results | `app/main.py` ADVERTISED_ACTIONS: the same six; each is in RUNTIME_ALLOWED_ACTIONS | PASS (identical) |
| R3 | CEG ingress allowlist | — | `docker-compose.prod.yml` L9_ALLOWED_ACTIONS adds admin, health, healthcheck, which Gate never routes | PASS (superset by design) |

## 3. Keys and key ids

Gate starts only if every caller-policy key id (`L9_KEY_ALLOWED_ACTIONS_JSON`)
has a verifying key (`L9_VERIFYING_KEYS_JSON`); CEG and EIE refuse Gate
signatures not made under `L9_GATE_SIGNING_KEY_IDS` (GATE-KEY-01).

| # | Key id | Holder | Needed in | Status |
|---|---|---|---|---|
| K1 | Gate's own signing key (id not yet chosen; ed25519) | Gate | CEG and EIE `L9_GATE_SIGNING_KEY_IDS` and their verifying keys; Odoo's verifying keys | **OPEN** — created at Gate onboarding |
| K2 | `enrichment-engine-v1` (node `enrichment-engine`) | EIE | Gate verifying keys + caller policy: worker, tenants `[plasticos]`, actions `health-assess`, `sync` (merged with any it already lists) | **OPEN** — public key not yet received. Key id and node match EIE `.env.example` (`L9_SIGNING_KEY_ID`, `L9_NODE_NAME`) |
| K3 | `odoo-plasticos-v1` (node `odoo`) | Odoo Production | Gate verifying keys + caller policy: consumer, tenants `[plasticos]`, actions `match`, `converge`, `enrichment-sweep`, `enrichment-sweep-results` | PASS for the key itself: ed25519, 32 bytes, valid base64. Odoo cannot load its signing env on Odoo.sh yet (CTO Odoo, open with the operator) |
| K4 | `graph-engine-v1` (node `graph`) | CEG | Gate verifying keys (Gate verifies CEG's signed responses) | **OPEN** — not yet in any recorded Gate entry; see F3 |
| K5 | `odoo-plasticos-staging-v1` | — | — | Dropped (operator option A, 2026-11-15): no staging Gate exists |

Tenant: `plasticos` for every entry. This matches CEG's identity rule (tenant id
== domain spec id == Neo4j database name, HEALTH-05) and the tracked PlasticOS
pack (`domain.id: plasticos`). Whether the deployed CEG serves a `plasticos`
pack and database is **UNVERIFIED** until CEG is running (`admin get_domain`).

## 4. GitHub settings

| # | Check | Gate | CEG | EIE |
|---|---|---|---|---|
| G1 | Required CI on default branch (org ruleset "L9 canonical CI required": org-ci.yml + "Analyze (central Core)") | PASS | PASS | PASS |
| G2 | Deletion and force-push protection on main | **FAIL** — none | **FAIL** — the "CI Gate" ruleset has no `include` refs, so it applies to no branch (F4) | PASS (`main-protection`) |
| G3 | Release tag protection for `refs/tags/v*` (the only refs the deployment profiles allow) | **FAIL** — no tag ruleset (F5) | **FAIL** — no tag ruleset (F5) | n/a (no profile yet) |
| G4 | Any `v*` tag exists | none — no release can be requested yet | none | n/a |
| G5 | Environments `release` and `prod`, required reviewers, branch policy | **UNVERIFIED** | **UNVERIFIED** | **UNVERIFIED** |
| G6 | Environment / repository secret and variable names | **UNVERIFIED** | **UNVERIFIED** | **UNVERIFIED** |
| G7 | GHCR package exists and Actions may publish to it | **UNVERIFIED** | **UNVERIFIED** | **UNVERIFIED** |

G5–G7: this session's GitHub proxy refuses the environments, secrets,
variables and packages endpoints (HTTP 403 "not permitted through this proxy").
They need an operator-side read (GitHub settings page, or `gh api` with an
admin token).

## 5. Findings

| # | Severity | Owner | Finding | Fix |
|---|---|---|---|---|
| F1 | High | CTO EIE | EIE sends `score-invalidate` through Gate after each enrichment (`app/engines/packet_router.py` `notify_score_invalidate`). No Gate code defines that action and Gate runs canonical-only (`GATE_CANONICAL_ACTIONS_ONLY: "true"` in #45), so every call is refused; it is fire-and-forget, so only EIE's log shows it. | Remove the call, or define and own the action in Gate. |
| F2 | Medium | CTO EIE | EIE with `L9_GATE_SIGNING_KEY_IDS` unset in staging/prod only logs an error and keeps accepting Gate claims (`app/core/config.py` GATE-KEY-01 note). CEG refuses to start in that case. | Fail closed in staging/prod, as CEG does. |
| F3 | Medium | CTO CEG | CEG's key `graph-engine-v1` is missing from the recorded Gate verifying keys (K4). If `graph_inference_feedback_enabled` is turned on, CEG also originates `graph-inference-result` and needs a caller-policy entry. | Add K4 to Gate's verifying keys at onboarding; add a policy entry only if that flag is turned on. |
| F4 | Medium | CTO CEG (Gate, CEG) | Gate main has no deletion/force-push protection; CEG's "CI Gate" ruleset targets no branch. | Add `~DEFAULT_BRANCH` to CEG's ruleset; add an equivalent ruleset to Gate. Operator action in repository settings. |
| F5 | High | CTO CEG (Gate, CEG) | Release is driven by `refs/tags/v*`, but no tag ruleset restricts who can create those tags. | Add a tag ruleset limiting `v*` creation to release owners. Operator action. |
| F6 | Low | Gate (pre-existing) | Gate main fails SonarCloud's quality gate on new-code security rating "C" (workflow pip installs, Dockerfile `--only-binary`, missing lock file, a path-traversal hotspot in a script). Pre-dates #43/#44. | Gate PR #34 covers part; workflow and Dockerfile edits need operator approval. |
| F7 | Medium | Template owner (unassigned) | `l9-release.yml` in Gate #45 and CEG #499 is hand-written, not the l9-deploy template; the template itself pins `container-release.yml@v1` (a movable tag). Scorecard flags the job-level write permissions. | Fix once in the template (per-job least privilege, SHA pin, documented need for packages/id-token/attestations); repos adopt verbatim copies. |
| F8 | Info | Operator | Both deployment profiles name C1 (`46.62.243.82`) as production, marked "PROPOSAL, not yet operator-confirmed"; the SSH host key must be re-verified over a trusted channel. | Operator confirmation before any release. |
| F9 | Info | Operator | CEG's `docker-compose.prod.yml` signs with `hmac-sha256`, so Gate must hold CEG's shared secret as its verifying key. Gate's own signature must be ed25519 (GATE-KEY-01); the nodes' algorithm is not decided. | Decide node signing algorithm (ed25519 recommended) before keys are generated. |

## 6. What clears deployment

1. T10 run and green.
2. K1, K2 and K4 generated and exchanged; K3's Odoo signing env solved.
3. F1–F5 fixed; F7 template owner named and template fixed.
4. G5–G7 confirmed by an operator-side read.
5. F8 and F9 decided by the operator.
