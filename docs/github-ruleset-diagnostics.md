# GitHub Org-Level Ruleset Enforcement — Diagnostic Commands

Diagnostic commands run while investigating whether/how GitHub org-level ruleset
enforcement was configured for `Quantum-L9`, before the org upgraded to
GitHub Enterprise. Extracted from the session transcript.

Context: the user clarified that an earlier "checking rules and rule sets"
comment referred to **GitHub repository rulesets** (branch protection / merge
gating), not the Cursor `.mdc` agent-governance rules. These commands
established: (a) what ruleset state currently exists across the org, (b) why
org-level rulesets were inaccessible (GitHub Free plan), and (c) what a
rollout would need to account for (CodeQL coverage, malformed ref-name
conditions, archived/fork/visibility status).

---

## 1. List rulesets on the current repo

```bash
cd "/Users/ib-mac/Dropbox/Repo_Dropbox_IB/Cognitive.Engine.Graphs"
echo "=== repo-level rulesets ===" && gh api repos/Quantum-L9/Cognitive.Engine.Graphs/rulesets
```

## 2. List org-level rulesets for Quantum-L9

```bash
echo "=== org-level rulesets (Quantum-L9 org) ===" && gh api orgs/Quantum-L9/rulesets
```

Result: 403 — org-level rulesets require GitHub Team/Enterprise; org was on
Free at the time.

## 3. Check org plan/billing tier and ruleset presence across repos

```bash
echo "=== org plan / billing tier ===" && gh api orgs/Quantum-L9 | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('plan:', d.get('plan')); print('name:', d.get('login'))"

echo
echo "=== list a few other Quantum-L9 repos and their rulesets ==="
for repo in $(gh repo list Quantum-L9 --limit 15 --json name -q '.[].name'); do
  echo "--- $repo ---"
  gh api repos/Quantum-L9/$repo/rulesets | \
    python3 -c "import json,sys; d=json.load(sys.stdin); print([r['name'] for r in d]) if isinstance(d,list) else print(d)"
done
```

## 4. Inspect full ruleset configs on this repo

```bash
echo "=== CI Gate (this repo) full detail ===" && \
  gh api repos/Quantum-L9/Cognitive.Engine.Graphs/rulesets/13800421 | python3 -m json.tool

echo
echo "=== Code Quality Copilot review ruleset detail (this repo) ===" && \
  gh api repos/Quantum-L9/Cognitive.Engine.Graphs/rulesets/19360468 | python3 -m json.tool
```

Finding: the "CI Gate" ruleset had `conditions.ref_name.include: []` /
`exclude: []` — an empty include array, which GitHub's create API rejects
(you must use `~ALL` or `~DEFAULT_BRANCH`), yet it existed via GET. Flagged as
a landmine to fix (`~DEFAULT_BRANCH`) before propagating this ruleset as a
template.

## 5. Inspect the org's special `.github` repo for existing propagation tooling

```bash
echo "=== quantum-l9-dotgithub structure ===" && \
  find "$HOME/quantum-l9-dotgithub" -maxdepth 3 -type f | sort
```

Found `ops/branch-protect.sh` — applies branch protection to that repo itself
only, not org-wide (rulesets are per-repo API objects; they are not
distributed automatically the way community-health files are via `.github`).

## 6. List all Quantum-L9 repos with archive/fork/visibility status

```bash
gh repo list Quantum-L9 --limit 200 --json name,isArchived,isFork,isPrivate,visibility | \
  python3 -m json.tool
```

## 7. Check CodeQL coverage and existing rulesets across every repo

```bash
for repo in Cognitive.Engine.Graphs l9-meta-injector Constellation.Gate LLM-Router SEO-Bot \
            Gate_SDK Website-Bot Enrichment.Inference.Engine l9-ci-core Cursor-Governance \
            l9-assurance l9-harness l9-ci-sdk igorbot l9-graphiti-memory l9-deploy \
            L9-Node-Template l9-constellation-topology .github l9-ci-debt-lsp \
            l9-ci-debt-intelligence l9-ci-debt-resolver L9-Graphite-Memory L9-Ops-MCP \
            l9-cognitive-runtime PR_Repair infisical-config l9-infra l9-tools \
            l9-repo-template Constellation.PackageTemplate DeckhouseOdoo Governance-Active \
            SustainabilitySolutions1 ai-agency-genesis-portal Quantum-Website-Cursor \
            quantum-dashboard SplitWisely.ai timeAutomation_v1.0; do
  has_codeql=$(gh api "repos/Quantum-L9/$repo/code-scanning/analyses" 2>&1 | \
    python3 -c "import json,sys; d=sys.stdin.read(); print('yes' if d.strip().startswith('[') and len(json.loads(d))>0 else 'no')" 2>/dev/null || echo "no/err")
  rulesets=$(gh api "repos/Quantum-L9/$repo/rulesets" 2>&1 | \
    python3 -c "import json,sys; d=json.load(sys.stdin); print(','.join([r['name'] for r in d]) if isinstance(d,list) else 'ERR')" 2>/dev/null || echo "ERR")
  echo "$repo | codeql_analyses=$has_codeql | rulesets=$rulesets"
done
```

Finding: only 7 of 29 public repos had CodeQL analyses running; most repos had
only the auto-generated "Code Quality Copilot review" ruleset; 3 repos
(`l9-meta-injector`, `l9-assurance`, `l9-graphiti-memory`) had zero rulesets.
This fed directly into the `scope` decision question (tiered rollout vs. full
CodeQL-required rollout vs. dry-run only).

## 8. Check raw error shape on a private-repo ruleset query

```bash
gh api repos/Quantum-L9/l9-harness/rulesets
```

## 9. Verify org plan upgrade and re-check org-level ruleset API access

```bash
echo "=== org plan now ===" && gh api orgs/Quantum-L9 | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('plan'))"

echo
echo "=== org rulesets API now unlocked? ===" && gh api orgs/Quantum-L9/rulesets
```

Run again after the user upgraded the org to GitHub Enterprise — confirmed the
`orgs/Quantum-L9/rulesets` endpoint was accessible, unblocking true org-level
ruleset enforcement (deferred until after the CI rollout is verified green,
per the `org_ruleset` decision: *"After rollout, once I've verified checks are
running green everywhere"*).

---

## Outcome

These diagnostics directly shaped three decisions surfaced via `AskQuestion`:

1. **Path**: write a rollout script vs. upgrade to GitHub Team — superseded by
   the user upgrading straight to Enterprise.
2. **Scope**: tiered ruleset rollout (baseline on all 29 public repos; full
   CodeQL gate only on the 7 with CodeQL already running) vs. full/dry-run.
3. **Org-ruleset timing**: apply org-level enforcement *after* rollout is
   verified green — not yet actioned; tracked as a follow-up once the CI
   preset rollout (see `l9-ci-core` / `l9-ci-sdk` work) is fully green across
   all 24 activated repos.
