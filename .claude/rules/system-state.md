
# System State

Update this file when merging PRs.

## Open PRs (seL4 Upgrade Program)
| PR | Wave | Title | Status |
|----|------|-------|--------|
| #57 | 1 | Invariant & Validation Hardening | OPEN |
| #58 | 2 | Refinement-Inspired Scoring | OPEN |
| #59 | 3 | Capability & Access Control | OPEN |
| #60 | 4 | State Management & Resilience | OPEN |
| #64 | 5 | Correctness Tooling & Verification | OPEN |
| #63 | 6 | Dormant Feature Activation | OPEN |
| #65 | — | seL4 Technical & Executive Documentation | OPEN |
| #66 | — | CLAUDE.md Comprehensive Revision | OPEN |

## Dormant Subsystems
| System | Blocker | Activation |
|--------|---------|------------|
| KGE (CompoundE3D) | kge_enabled=False | Wave 6 merge → trigger_kge admin subaction |
| GDPR erasure | erase_subject() unwired | Wave 6 merge → erase_subject admin subaction |
| PostgreSQL persistence | Not in docker-compose | Provision PostgreSQL → wire asyncpg.Pool |
| LLM security | FeatureNotEnabled | Set LLM_PROVIDER + LLM_API_KEY |
| `l9-harness` (Quantum-L9 constellation) | Not adopted — only `l9-ci-core` (`Analyze (central Core)`) and `l9-ci-sdk` (PacketEnvelope ledger) are wired; see `docs/CI_CONSTELLATION_BOUNDARY.md` | Explicit human decision; must layer above `audit.yml`, never replace it |
| `l9-assurance` (Quantum-L9 constellation) | Not adopted — verdict/fan-in role currently played by `ci.yml` CI Gate + `ci-quality.yml` quality-gate | Explicit human decision; see `docs/CI_CONSTELLATION_BOUNDARY.md` |

## Pending Operator Actions
| Action | Why | Blocks | Logged |
|--------|-----|--------|--------|
| Upgrade the production Neo4j account to Enterprise Edition 5.7+ | Relationship evidence sync creates a per-type `(_tenant, evidence_ref)` relationship uniqueness constraint, which only Enterprise supports; it is what stops two concurrent writers both committing one `evidence_ref` | Relationship evidence sync in production (`relationship_sync_enabled`, default on): fails closed with `ExecutionError`, nothing written, until upgraded. Node sync unaffected | 2026-10-30, QM.POC0 close-out (#343); see `docs/FEATURE_GATES.md` §28 |
| Move Gate to ed25519 signing and set CEG `L9_GATE_SIGNING_KEY_IDS` in staging/prod | GATE-KEY-01: CEG now accepts a Gate-claiming packet only under a Gate key id and refuses hmac-sha256 there, closing tenant injection by any verified peer | CEG startup in staging/prod until set; Gate's Docker e2e rail (`tests/e2e/docker/compose.yml`) until it signs ed25519 | 2026-10-31, SCORING-GATE-01; see `docs/FEATURE_GATES.md` §29 |
| Enable remote score for a specific approved tenant | `REMOTE_SCORE_ENABLED` is off everywhere; needs a reviewed spec with `score:read`, a Gate consumer policy naming `score` and the tenant, Neo4j Enterprise | Gate-routed `score` | 2026-10-31, SCORING-GATE-01 |
