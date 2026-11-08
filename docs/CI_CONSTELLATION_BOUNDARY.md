<!-- L9_META
l9_schema: 1
origin: ceg
engine: graph
layer: [ci]
tags: [ci, constellation, l9-ci-core, l9-ci-sdk, l9-harness, l9-assurance, boundary]
owner: platform
status: active
-->

# CI_CONSTELLATION_BOUNDARY.md — Quantum-L9 CI Constellation Boundary

**Read this before wiring `l9-ci-core`, `l9-ci-sdk`, `l9-harness`, or `l9-assurance`
into this repo, or before proposing any change that touches
`tools/audit_harness.py`, `.github/workflows/audit.yml`, or
`tools/packet_envelope_gate.py`.**

## The non-negotiable rule

> **Extend, never replace.** This repo's own audit/CI tooling
> (`tools/audit_harness.py`, `.github/workflows/audit.yml`) is not a stand-in
> for the constellation and must not be deleted, gutted, or silently
> superseded when constellation adoption expands. If/when `l9-harness` is
> ever adopted here, it sits **above** `audit.yml`, orchestrating the
> existing harness as one observation source among several — it does not
> absorb, rewrite, or disable it. Any change that would remove or bypass
> `audit_harness.py`'s CI-gating role requires an explicit human decision,
> not an autonomous agent action.

## Current state: what's already wired (real, not theoretical)

Two of the four constellation repos are integrated. `l9-ci-core` runs as the
organization-required check `Analyze (central Core)` (org ruleset "L9 canonical
CI required"), which executes this repo's Repository Execution V2 contract
(`.l9/repo-workflow.json`, the generated `Makefile` facade and `Repo.mk`, #413).
CEG adds no caller for it. `l9-ci-sdk` is pinned to an immutable commit SHA
behind a "thin caller" (this repo supplies inputs only; all logic lives
upstream):

| Constellation repo | Where it's wired | Purpose | Pinned revision |
|---|---|---|---|
| `Quantum-L9/l9-ci-core` | Organization ruleset (no file in this repo) + `.l9/ci.json`, `.l9/repo-workflow.json` | `Analyze (central Core)`: Semgrep analysis, SDK admission, governance, and the V2 setup/validate/check/test phases | Core-controlled (`main`) |
| `Quantum-L9/l9-ci-sdk` | `tools/packet_envelope_gate.py` (provisioned via shallow clone into gitignored `.l9/runtime/sdk/`) | `l9_ci baseline scan-packet-envelope` + `compare-scan` — the deterministic AST scanner behind the `packet-envelope-prohibited` pre-commit hook, which CI runs in `ci.yml`'s `precommit` job | `0779fca8238011f8abea551895f96584676e9d17` |

Governed ledgers (human-owned, CODEOWNERS-protected, **never** written by CI
or an agent):

- `.l9/baselines/packet-envelope.yml` — deprecated `PacketEnvelope` debt, one
  entry per owner/issue/expiry (see `docs/contracts/SHARED_MODELS.md` for the
  `TransportPacket` migration this ledger tracks)

**Retired 2026-11-08 (#335):** `.github/workflows/baseline-ratchet-caller.yml`
and `.l9/baselines/test-quarantine.yml`. The caller pinned Core's v1
`baseline-ratchet.yml`, which Core retired (its 2026-10-22 legacy-retirement
report), so the workflow failed with no jobs on every push. No ruleset
required its `Baseline Ratchet / *` checks. The test-quarantine ledger was empty and read only by
that caller. The PacketEnvelope ban is unaffected: it is the
`packet-envelope-prohibited` hook, run locally and by CI's `precommit` job.

**Scope of the SDK integration: narrow.** It exists solely to run the
PacketEnvelope-debt ledger comparison. It does **not**
mean "CI runs through the constellation" — `ci.yml`, `ci-quality.yml`, and
`audit.yml` remain fully independent, CEG-owned pipelines. See
`docs/CI_PIPELINE.md` for the full 8-phase pipeline this sits inside.

### CEG-owned extensions of `ci.yml`

New CEG checks extend `ci.yml` (CEG-owned) rather than `audit.yml` or a
constellation repo:

| Added | Job | What it does | Gating |
|---|---|---|---|
| 2026-10-29 | `integration` — *Integration Tests (Neo4j Enterprise + PostgreSQL)* | Runs `pytest tests/integration -p no:xdist` against `neo4j:5.18-enterprise` (GDS plugin, Enterprise evaluation licence accepted for CI, owner-approved) and `postgres:16` service containers, with `CEG_REQUIRE_INTEGRATION=1` so `tests/conftest.py` fails instead of skipping when a service or dependency is missing | Advisory: not in the `ci-gate` fan-in and not a required check |

## What's NOT wired: `l9-harness` and `l9-assurance`

Neither repo is integrated here. The only mention of `l9-harness` anywhere
in this repo is a passing name in a loop in
`docs/github-ruleset-diagnostics.md` (an org-wide ruleset audit) — not a
functional call, dependency, or CLI invocation.

| | `Quantum-L9/l9-ci-core` + `l9-ci-sdk` (wired above) | `Quantum-L9/l9-harness` (not wired) |
|---|---|---|
| Role in the 4-party authority model | CI Core orchestrates/publishes; CI SDK executes checks and emits observations | Exercises public contracts, preserves bytes, deterministic local execution/replay/shadow-comparison — explicitly **never** a required hop, never publishes GitHub checks, never issues verdicts |
| Scope | One narrow gate (PacketEnvelope debt ledger) plus Core's org-required analysis | Whole-constellation conformance/replay/corpus-sync tool, if ever adopted |
| Maturity (as of 2026-07-24) | Actively running, gating merges today | Private repo, created 2026-07-04, self-described fail-closed pending upstream authority (`BUILD_AUTHORIZATION.md`, `VALIDATION.md`) |
| `l9-assurance` | Not present at all — that's the party that would eventually "admit evidence and issue verdicts," a role currently played ad hoc by the `CI Gate` / `quality-gate` fan-in jobs in `ci.yml` / `ci-quality.yml` | Not present |

Full technical comparison against CEG's own `tools/audit_harness.py`
(different tool, different purpose — a single-repo static-analysis
orchestrator, not a constellation component) is preserved in the session
transcript; ask for it again if needed rather than assuming this doc
duplicates it.

## If an agent is asked to instantiate `l9-harness` or `l9-assurance` here

1. **Do not touch** `tools/audit_harness.py`, `docs/AUDIT_HARNESS.md`, or the
   `Post harness report to PR` step in `.github/workflows/audit.yml` as part
   of that work. Those stay as-is regardless of constellation adoption.
2. **Follow the existing pattern exactly** — it is already proven in this
   repo:
   - Pin the constellation repo to an immutable commit SHA (never a branch
     or tag that can move).
   - Add a **thin wrapper/caller** (see `tools/packet_envelope_gate.py`) that supplies
     repo-specific inputs only — zero scanning/comparison logic lives
     locally.
   - Provision any cloned SDK/harness code into a gitignored path under
     `.l9/runtime/` (see `.gitignore` line for `.l9/runtime/`) — never
     commit vendored constellation code.
   - Any ledger the new gate produces is human-owned and CODEOWNERS-gated
     (see `.github/CODEOWNERS` entry for `/.l9/baselines/`); CI and agents
     read ledgers, never write them.
3. **Layer above `audit.yml`, don't fold into it.** If `l9-harness` reaches
   the point of orchestrating local execution/replay across this repo's
   checks, treat `audit_harness.py`'s three checks (`audit_engine.py`,
   `spec_extract.py`, `verify_contracts.py`) as one SDK-level observation
   source it *collects*, not a target it rewrites or a workflow it deletes.
4. **`l9-assurance` adoption is a bigger decision than the others.** It
   would change who "issues verdicts" for this repo — currently that's the
   `CI Gate` fan-in in `ci.yml` and the `quality-gate` fan-in in
   `ci-quality.yml` (see `docs/CI_PIPELINE.md`). Do not adopt it
   autonomously; this requires an explicit human decision given its
   pre-production status upstream (`l9-harness`'s own docs list required
   authority records — release commit, schema digests, SDK identity — as
   not yet supplied).
5. If genuinely unsure whether a proposed change counts as "instantiating
   the constellation" vs. "extending the existing narrow integration,"
   stop and ask rather than guessing.

## Related documents

- `docs/CI_PIPELINE.md` — the 8-phase CI pipeline this integration sits
  inside; source of truth for blocking vs. advisory jobs
- `docs/AUDIT_HARNESS.md` — what `tools/audit_harness.py` does and doesn't do
  (L9 template doc, shared across L9 repos — do not add constellation-specific
  content there; it belongs here instead)
- `docs/contracts/SHARED_MODELS.md` — the `PacketEnvelope` → `TransportPacket`
  migration the packet-envelope ledger tracks
- `docs/github-ruleset-diagnostics.md` — the org-wide ruleset rollout
  investigation where `l9-harness` was mentioned only as a repo name, not a
  dependency
- `.claude/rules/system-state.md` — tracks `l9-harness`/`l9-assurance` as
  dormant (unwired) constellation members
