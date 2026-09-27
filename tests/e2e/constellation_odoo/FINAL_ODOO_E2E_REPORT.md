# Odoo on the Constellation Docker rail — diagnosis, results, fixes

> **Status (after fixes, 2026-09-26):** approved fixes 1–5 (§7) are applied in
> their owning repositories. With pristine images built from those revisions,
> **the Odoo rail PASSES 19/19 mandatory checks** (run `20260926T191817Z`)
> and **the Gate 3-node rail PASSES 21/21** (run `20260926T192218Z`), with no
> image accommodation. §0–§6 below are the **pre-fix** diagnosis
> (run `20260926T184524Z`) and are kept as the record that justified the fixes.

## 0. Verdict

| Rail | What it proves | Result |
|---|---|---|
| Gate rail (3 nodes), pristine images at current heads | Gate ⇄ EIE ⇄ CEG under mandatory signatures | **FAIL: 15/21.** EIE's image exits on boot (F-E1). Every EIE-dependent check fails. |
| Gate rail (3 nodes), EIE accommodated (F-E1 only) | same | **PASS: 21/21** |
| **Odoo rail** (this overlay), `Odoo → Gate → EIE → Gate → Odoo` | consumer admission, round trip, business path, adversarial | **FAIL: 16/17 mandatory.** The only failure is O_B3 (Inject writes nothing, F-O1). Its fix is proven by O_P1. |

Plain answers to the four questions asked:

1. **Can Odoo reach nodes behind Gate?** **Yes.** Real Odoo 19 with the
   `plasticos_gate` addon, using the real `GateClient.execute` path from Gate_SDK
   `e9f829f`, round-trips `converge` through Gate to EIE and back. The reply is
   re-signed by Gate (`gate-e2e`) and verified by Odoo (O_T1). With a completing
   provider, the operator's **Execute** button lands the run in `review` with a
   Gate packet id and EIE provenance (O_B1). A duplicate delivery of the same
   operation is answered from Gate's idempotency cache with the same `packet_id` (O_B4).
2. **Does Odoo need to register with Gate?** **No, and it can't.**
   Registration (`POST /v1/admin/register`, `X-Admin-Token`) is for workers that
   *execute* actions. A consumer needs **one thing**: its signing key id listed in
   Gate's `L9_VERIFYING_KEYS_JSON`. Odoo's attempt to register itself as the
   `converge` owner is refused with 401 (O_N4).
3. **Is there a layer that stops "any random consumer with Gate_SDK"?**
   **Yes, but it is coarse.** With `L9_REQUIRE_SIGNATURE=true` (mandatory in
   staging/prod) Gate rejects the following. Each was proven live:
   - unsigned packets (O_N1);
   - unknown key ids (O_N2, the "random SDK install" case);
   - forged signatures (O_N3);
   - direct worker bypass (O_N5).

   **What it does not do:** once a key is admitted, Gate authorizes nothing
   further (F-G1…G4). The admitted Odoo key could:
   - call CEG's `sync` write action and create a Facility node in CEG's graph;
   - send packets as `enrichment-engine`;
   - send packets for another tenant;
   - read every worker's internal URL from an unauthenticated endpoint.

   No per-consumer registry exists in any repository.
4. **How far did Odoo get?** The transport and the operator flow are done. Two
   cross-repo **contract** defects keep it from being useful end to end:
   - enrichment proposals carry nothing writable (F-O1);
   - matching is rejected by CEG for every Odoo request (F-O2).

## 1. Revision set (all on `claude/odoo-gate-sdk-integration-yg6osh`)

| Repo | HEAD | Clean |
|---|---|---|
| Constellation.Gate | `a90fb0b3c424` | yes |
| Enrichment.Inference.Engine | `b583c9edc05e` | yes |
| Cognitive.Engine.Graphs | `e1983c6c614d` (= main `ebe7795` + this harness) | yes |
| IB-Odoo_19 | `f53d3cdff5bd` | addon tree clean. 46 `.claude/skills/*` symlinks were rewired by the session's governance bootstrap; no `plasticos_*` byte differs. |
| Gate_SDK | `17bffaa3f164` | yes |

**SDK alignment (read from each running image's `dist-info`, not from lockfiles):**
Gate, EIE, CEG and Odoo all install `constellation-node-sdk 1.1.0 @ e9f829f98211`
(the `v1` channel). All four are **aligned**.

## 2. Where the harness lives

The existing cross-repo Docker rail lives in **Constellation.Gate**
(`constellation-gate/tests/e2e/docker/`), not in CEG. CEG had no e2e harness;
two CI configs exclude a `tests/e2e/` directory that did not exist. This work
adds `tests/e2e/constellation_odoo/` to CEG as an **overlay** on the Gate rail,
so the three-node configuration is reused and not forked. Results are published
here under `results/`: one redacted `.tar.gz` per run, with verdicts verbatim in `results/SUMMARY.md`. Commands are in `README.md`.

## 3. Results

### 3.1 Odoo rail — `results/odoo-rail-20260926T184524Z.tar.gz`

| Check | Result | Evidence |
|---|---|---|
| O_INSTALL | PASS | `plasticos_gate 19.0.1.9.1`, `plasticos_enrichment 19.0.2.5.0`, `plasticos_matching 19.0.3.1.0` + cascade: 264 modules, registry loaded in 160 s, no errors |
| O_CONFIG | PASS | signing configured from process env only (no secret in `ir.config_parameter`) |
| O_ISOLATION / DOCKER_ISOLATION | PASS | Odoo resolves `gate` only. `enrichment-engine`, `graph`, `neo4j` and `eie-postgres` don't resolve, and EIE can't resolve `odoo`. |
| O_T1_ROUNDTRIP | PASS | `response` packet, `signing_key_id=gate-e2e`, idempotency key preserved end to end |
| O_T2_OPERATOR_FAIL_CLOSED | PASS | Execute on an empty EIE answer gives `degraded` / `unknown`, never `injected`, no local fallback |
| O_B1_CONVERGE_REVIEW | PASS | `review`, `gate_packet_id` set, 10 EIE provenance keys |
| O_B2_NO_WRITE_BEFORE_APPROVAL | PASS | partner untouched until Inject |
| **O_B3_APPROVE_INJECT** | **FAIL** | `UserError: Gate proposal has no writable partner fields after allowlist filtering` (F-O1) |
| O_B4_IDEMPOTENT_REPLAY | PASS | two sends of one operation return the same `packet_id` |
| O_N1…N5 | PASS | unsigned: Gate 400 (`signature required`); unknown key id: Gate 400; forged: Gate 400; self-register: 401; direct EIE: DNS failure |
| O_M1_MATCH_ODOO_CONTRACT | **FAIL** | CEG `ValidationError`: `No candidate entity for direction 'intake_to_buyer'` (F-O2) |
| O_M2_MATCH_CEG_SPEC_DIRECTION | PASS | same request, `supply_opportunity_to_buyer_facility`, returns a `response` with `candidates` |
| O_P1 (proposal probe) | PASS | Odoo request + `schema` from Odoo's own allowlist: EIE returns 8 fields; the real Inject writes 7 fields (the six checked, city · email · phone · street · website · zip, all filled), creates **7 provenance rows**, and keeps the existing `name` |
| O_F1 | GAP | F-E3 |
| O_G1…G4 | GAP ×4 | F-G1…G4. The G1 write is visible in Neo4j: `E2E-ODOO-AUTHZ-PROBE` (`flows/neo4j_odoo_authz_probe.txt`) |
| EVIDENCE_no_secrets | PASS | every env value scanned across the bundle; 0 leaks |

### 3.2 Gate rail baselines — `results/gate-rail-*.tar.gz`

- `…181056Z-pristine-heads` **FAIL**. The EIE container exits with `ImportError: …requires that the Python 'greenlet' library is installed` (`logs/eie.log.txt`). These checks fail: P1, P5, P6, N6, REG and PERSIST. Every Gate⇄CEG check and every signature, replay and forgery negative passes.
- `…182720Z-eie-accommodated` **PASS 21/21**, including all three provenance gates. The only change is the `sqlalchemy[asyncio]` layer.

## 4. Findings

Severity: **S1** blocks the Odoo use case or is a security boundary gap · **S2** correctness · **S3** hygiene.

### Gate: consumer authorization (the question asked)

| ID | Sev | Finding | Code | Live evidence |
|---|---|---|---|---|
| F-G1 | S1 | No per-consumer **action** scope. Any admitted key can call any routed action, including worker write actions. | `boundary/routing_policy.py:27` (`del known_nodes`). The only action filter is the global `L9_ALLOWED_ACTIONS`. | O_G1: Odoo's key ran CEG `sync` and wrote a Neo4j node |
| F-G2 | S1 | A key id is not bound to a **node identity**. A holder can claim any `source_node`, and the per-caller rate limit keys on that self-declared name. | `config/settings.py:295-306` (key lookup by id only); `services/execute_service.py:138` | O_G2: Odoo's key accepted as `enrichment-engine` |
| F-G3 | S1 | A key id is not bound to a **tenant**. `tenant` is caller-supplied, and Gate only uses it to namespace idempotency. | `transport/tenant.py` (SDK), no Gate check | O_G3: Odoo's key accepted for `some-other-tenant` |
| F-G4 | S2 | `GET /v1/registry` is unauthenticated and returns every worker's `internal_url`. | `api/main.py:126-132` | O_G4 |
| F-G5 | S3 | The admin token is compared with `!=`, not in constant time. | `services/admin_registration_service.py:67` (`_authorize`) | code read |
| F-G6 | S3 | `resolve_verifying_key` falls back to Gate's own signing key, so anything holding Gate's key can enter as any identity. The Gate rail's own driver relies on this (it signs as `gate-e2e`). | `config/settings.py:300-305` | code read |

### Odoo ↔ EIE / CEG contract

| ID | Sev | Finding | Code | Live evidence |
|---|---|---|---|---|
| F-O1 | S1 | `build_converge_request` sends no `schema`. EIE fills **only** the fields named in `EnrichRequest.schema`, so it echoes the input and Inject has nothing to write. | IB-Odoo_19 `plasticos_gate/services/gate_builders.py:117-163`; EIE `app/engines/enrichment_orchestrator.py:86`, `app/models/schemas.py:47` | O_B3 FAIL; fix proven by O_P1 |
| F-O2 | S1 | Odoo's `MatchRequest.match_direction` defaults to `intake_to_buyer`, a direction CEG's plasticos spec doesn't declare. **Every** Odoo match fails. | IB-Odoo_19 `plasticos_gate/services/gate_contracts.py:99`; CEG `domains/plasticos/spec.yaml:34`, `engine/handlers.py:465-468` | O_M1 FAIL / O_M2 PASS |
| F-O3 | S3 | On the non-ok path Odoo logs "falling back to local" (no fallback exists), and the durable degraded record drops `gate_packet_id` / `gate_correlation_id`. | `plasticos_enrichment/models/enrichment_run.py:184`, `:389-401` | O_T2 record has `gate_packet_id: null` |

### EIE packaging and contract

| ID | Sev | Finding | Code | Live evidence |
|---|---|---|---|---|
| F-E1 | S1 | The image doesn't boot. `Dockerfile` installs `.[dev]` from `pyproject.toml` (unpinned `sqlalchemy` resolves to 2.1.1, where greenlet is behind `[asyncio]`) and ignores `requirements.lock` (2.0.54 + greenlet). | `Dockerfile` (`pip install ".[dev]"`); `pyproject.toml:31` | pristine baseline FAIL |
| F-E2 | S1 | `alembic` isn't a dependency, and nothing runs migrations at startup. On a fresh database every durable `converge` fails: `relation "enrichment_results" does not exist`. | `pyproject.toml`; `migrations/`; `app/services/pg_store.py:64-69` | first Odoo run (pre-migration) |
| F-E3 | S2 | With no provider reachable (0 tokens, confidence 0.0, uncertainty 1.0, no fields), canonical `converge` returns `state=completed` and no `failure_reason`. EIE reports success on an empty result. Odoo's empty-allowlist guard catches it; other consumers may not. | convergence loop logs `converged_insufficient_improvement` then `handlers.converge_ok` | O_F1 |

### Stale claims retired by this run

- The committed `IB-Odoo_19/FINAL_FINDINGS.md` says Gate_SDK has no `execute()` and that Odoo builds packets. That is stale: Odoo now calls `GateClient.execute` (`gate_client.py:250`).
- The same file says `real_odoo_19` / `real_gate` were `NOT_RUN`. They have now been run.
- `Constellation.Gate/PR_FINDINGS_BRIEF.md` says EIE and Odoo are pinned to older SDK commits. That is stale: all four run `e9f829f`.
- The Gate rail's `compose.yml` comment says EIE's Dockerfile runs `--workers 4`. That is stale: it runs `--workers 1`.
- *The "upload" with remediations mentioned in the request was not attached to this session.* The committed findings files were used instead, and they are reconciled above.

## 5. Declared test deviations (printed by the verdict)

1. One CA-trust layer per image stage (inherited from the Gate rail).
2. EIE image accommodation: `sqlalchemy[asyncio]`, `alembic`, layered on the unmodified image. Each is applied only after a probe shows it missing (F-E1, F-E2).
3. EIE runs `alembic upgrade head`, its documented schema step, as an explicit deploy step.
4. The business phase runs EIE with `L9_ENVIRONMENT=test` + the deterministic provider. The live provider needs a key and egress, and the deterministic source is refused in staging by design. Gate stays `staging` with mandatory signatures.
5. `plasticos.gate.allow_insecure_http=1`. Plain HTTP runs inside the Docker network; integrity comes from the HMAC signature.

## 6. Proposed fixes (pre-fix proposal; see §7 for what was applied)

Ordered by leverage. Each is scoped to the repository that owns the concern.

| # | Repo | Change | Closes | Verification |
|---|---|---|---|---|
| P1 | **Constellation.Gate** | **Consumer admission registry.** Add `GATE_CALLER_POLICY_JSON` (or a YAML beside `node_registry.yaml`): `{key_id: {node, kind: consumer\|worker, tenants: [...], actions: [...]}}`. Enforce it in `IngressValidator` right after signature verification: key id → allowed `source_node`, `tenant.org_id` and action set. Fail closed in staging/prod when a verified key id has no binding. Consumers are provisioned by adding a key id and its policy; they never call `/v1/admin/register`. | F-G1, F-G2, F-G3 (and makes F-G6 explicit) | O_G1–G3 flip from GAP to ENFORCED; O_N1–N5 stay PASS |
| P2 | Constellation.Gate | Require `X-Admin-Token` on `/v1/registry` and `/v1/capabilities`; use `hmac.compare_digest` for the token. | F-G4, F-G5 | O_G4 becomes ENFORCED |
| P3 | IB-Odoo_19 | `build_converge_request`: send `schema` derived from `PARTNER_WRITEBACK_FIELD_ALLOWLIST`, so Odoo asks for exactly what it may write. | F-O1 | O_B3 PASS (proven today by O_P1) |
| P4 | IB-Odoo_19 **or** CEG | Align `match_direction`. Either Odoo's default becomes `supply_opportunity_to_buyer_facility`, or CEG's plasticos spec declares `intake_to_buyer` as an alias. Recommended: fix Odoo, since CEG's spec is the SSOT per CEG's design principle 1. | F-O2 | O_M1 PASS |
| P5 | Enrichment.Inference.Engine | Pin `sqlalchemy[asyncio]` in `pyproject.toml` and add `alembic`. Better: the Dockerfile installs from the hash-locked `requirements.lock` and then `pip install --no-deps .`. | F-E1, F-E2 | pristine baseline PASS without accommodation |
| P6 | Enrichment.Inference.Engine | Run `alembic upgrade head` as an explicit release step (entrypoint or init job), not implicitly in the app. | F-E2 | fresh-DB converge durable |
| P7 | Enrichment.Inference.Engine | `converge` with zero provider responses must return `state=failed`, `failure_reason=no_valid_responses`. | F-E3 | O_F1 becomes ENFORCED |
| P8 | IB-Odoo_19 | Keep `gate_packet_id` / `gate_correlation_id` on the degraded record; fix the stale log text. | F-O3 | O_T2 record carries the packet id |

Once P1, P3, P4 and P5 land, re-running `run_odoo_e2e.sh` is expected to give an
unqualified **PASS** with no EIE accommodation. That remains a prediction until the run happens.

## 7. Applied fixes and after-fix proof

Fixes 1–5 (approved), each in its owning repository on
`claude/odoo-gate-sdk-integration-yg6osh`. P2, P7, P8 and the per-key
identity/tenant binding were **not** in scope and are unchanged.

| # | Repo | Commit | Change |
|---|---|---|---|
| 1 | Constellation.Gate | `8885140`, `d64d58e` | `L9_KEY_ALLOWED_ACTIONS_JSON`: a verified key id listed there may invoke only its actions (else `403 action_not_permitted`); unlisted key ids unchanged; a scope for an unknown key id fails startup; scopes require `L9_REQUIRE_SIGNATURE=true` and unsigned packets are refused while scopes exist (`d64d58e`, review). Odoo: `{"odoo-k1": ["converge","match"]}`. |
| 2 | Cognitive.Engine.Graphs | `ba1b340` | `GraphLifecycle` re-runs the existing `register_from_env()` every `gate_reregistration_interval_seconds` (300) behind `gate_reregistration_enabled` (default on, FEATURE_GATES §17). |
| 3 | Enrichment.Inference.Engine | `c199c52`, `3fbf353`, `3b4108e`, `3ffa180` | `sqlalchemy[asyncio]` + `alembic>=1.13` declared; lock regenerated (+alembic, mako, markupsafe only); the dev image's `scripts/docker-entrypoint.sh` runs `alembic upgrade head` before the unchanged CMD; production applies it as an explicit operator step (`3ffa180`, review: AGENTS.md forbids it in production context); `requirements-ci.txt` declares the same (`3b4108e`); cross-repo fixture mirrors Odoo's `schema`. |
| 4 | IB-Odoo_19 | `9c5cdf4` | `ConvergeRequest.schema` = `PARTNER_WRITEBACK_FIELD_ALLOWLIST` (`{field: "string"}`). |
| 5 | IB-Odoo_19 | `9c5cdf4` | `MatchRequest.match_direction` default = `supply_opportunity_to_buyer_facility`; `plasticos_gate` 19.0.1.9.2. |

| Check | Before | After |
|---|---|---|
| Gate rail, pristine heads | FAIL 15/21 (EIE does not boot) | **PASS 21/21**, no accommodation, no key scope configured (fix 1 backward compatible) |
| O_B3 Approve → Inject | FAIL (nothing writable) | **PASS** |
| O_M1 match, Odoo's own contract | FAIL (`No candidate entity for direction 'intake_to_buyer'`) | **PASS** |
| O_G1 Odoo key → CEG `sync` | GAP (accepted, wrote Neo4j) | **ENFORCED** (`403 action_not_permitted`) |
| GATE_RESTART_RECOVERY (new) | — (manual repro: CEG never returned, match 404) | **PASS**: EIE and CEG back in the registry ~4 s after Gate restart (test interval 10 s); Odoo match passes through the recovered Gate |
| EIE image accommodation | `sqlalchemy[asyncio]`, `alembic` | **none** (probe: not needed) |

Unit suites on the changed trees: Gate 505 passed; CEG 1883 passed; EIE 1784
passed; Odoo 909 passed. Remaining reported, unscoped findings: O_F1 (EIE empty
result reported `completed`), O_G2/O_G3 (key not bound to node name / tenant),
O_G4 (unauthenticated `/v1/registry`).

## 8. Gate_SDK participation closure (L9-PARTICIPATION-01)

The fixes above still left each participant doing its own Gate integration:
- EIE and CEG each carried a registration loop, CEG's added as fix 2.
- EIE had registration-aware readiness of its own.
- Odoo classified Gate failures itself.
- A consumer learned whether Gate admitted it only from a 403 on a real call.

That machinery now lives in Gate_SDK (Quantum-L9/Gate_SDK#55). Gate gained the
matching admission probe (`POST /v1/admission`, Constellation.Gate#25,
`b8cf238`). The rail proves both in its SDK participation mode
(`L9E2E_SDK_PARTICIPATION=1`, see README).

### Run under test

- **Run:** `results/odoo-rail-20260927T022821Z-sdk-participation.tar.gz` — **VERDICT PASS**, 27/27 mandatory.
- **Image sources:** every image runs Gate_SDK `eaac6a8`: gate `b8cf238`, EIE `3ffa180`, CEG `db8fdb4`, Odoo `9c5cdf4`, sdk-node = Gate_SDK `eaac6a8`.
- **EIE and CEG** run with their own registration code removed (`patches/*-sdk-adoption.diff`, a labelled image layer). Their participation is the SDK's.
- **`sdk-minimal-node`** is Gate_SDK `examples/minimal_node`: handlers, a spec file and `create_node_app()`. It has no Gate integration code.

### Checks

| Check | Result |
|---|---|
| P_SDK_NODE_ACTIVE | EIE, CEG and the minimal node are all `active` with `/v1/ready` 200; all three are in Gate's registry. |
| P_SDK_NODE_ROUTABLE | The minimal node's `sdk-echo` goes out through Gate and back to the node. It returns Gate-signed (`gate-e2e`) with `{"echo": {"probe": "l9e2e"}}`. |
| P_SDK_NODE_RECOVERY | Gate is stopped. All three nodes turn `/v1/ready` 503 `degraded`, observed 9 s into the outage. When Gate is back they re-register (minimal node 4 s, EIE 6 s, CEG 10 s; interval 10 s) and return to 200 `active`. No node code is involved. |
| C_ADMISSION_RECEIPT | Odoo's production config builder plus `GateClient.activate()` returns key `odoo-e2e`, `restricted`, granted `converge` and `match`. Nine actions are routable. |
| C_REQUIRED_ACTION_MISSING | Requiring `sync` raises `GateAuthorizationError(code="action_not_permitted")`, not retryable. |
| C_ADMISSION_UNKNOWN_KEY_REJECTED | An unknown key gets 400 `invalid_transport_packet`. It is not reported as an authorization error. |
| C_TYPED_403 | An out-of-scope `sync` raises a typed `GateAuthorizationError` (403 `action_not_permitted`). |
| All 20 previous mandatory checks | PASS, including O_B3, O_G1 ENFORCED, GATE_RESTART_RECOVERY and PROVENANCE. |

### Declared deviations

These are printed by the verdict:
- the Gate_SDK overlay on every image;
- the EIE and CEG adoption diffs;
- EIE's deterministic business phase.

The overlay and the diffs go away when Gate_SDK 1.2.0 is on `@v1` and EIE and
CEG adopt it in their own repositories.

### After approval

1. **EIE and CEG adopt the SDK.** The adoption diffs become their PRs: `create_node_app(registration=...)`, with their loops deleted. After that they also delete their now-dead registration modules and tests.
2. **Odoo adopts the SDK.** Odoo replaces `classify_transport_failure` with `err.retryable` and calls `activate()` in its configuration check.
3. **Release.** Gate_SDK 1.2.0 is released and promoted to `@v1`.
4. **Acid test.** A new node, such as the Reconciler, is born with only `create_node_app()`.
