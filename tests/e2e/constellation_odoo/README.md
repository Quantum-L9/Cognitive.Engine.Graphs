# Constellation Docker rail — Odoo consumer overlay

Adds **IB-Odoo_19** to the Constellation release-set system E2E as a
**consumer**: `Odoo -> Gate -> EIE -> Gate -> Odoo` (and `Odoo -> Gate -> CEG`
for `match`), with Odoo running as a real Odoo 19 server. The addons are
installed by Odoo itself, and the scenarios run inside the real registry through
the same `plasticos_gate` services Odoo uses in production.

This is an **overlay** on Constellation.Gate's rail
(`constellation-gate/tests/e2e/docker/`). It does not copy that rail. Gate,
EIE and CEG are configured exactly as that rail configures them. The only
change on the Gate side is that one consumer key id (`odoo-e2e`) is added to
Gate's verifying keyring.

```
            control_net                        (driver / diagnostics)
                 │
 odoo ── odoo_gate_net ── gate ── gate_eie_net ── enrichment-engine ── eie_data_net
  │                        │                                           (redis, postgres)
odoo_data_net              └──── gate_ceg_net ── graph ── ceg_data_net (neo4j)
 (odoo-db)
```

Odoo shares a network with Gate and nothing else, so it cannot resolve
`enrichment-engine` or `graph`. The rail asserts this both from inside Odoo
(`O_ISOLATION`) and against Docker (`DOCKER_ISOLATION`).

## Files

| File | Role |
|---|---|
| `compose.odoo.yml` | Overlay: `odoo` + `odoo-db`, the `odoo_gate_net` / `odoo_data_net` networks, and Gate's widened keyring |
| `compose.eie-deterministic.yml` | Business-path overlay: recreates only EIE with the in-repo deterministic enrichment source |
| `scripts/build_images_odoo.sh` | Builds `l9e2e/odoo:local` from `IB-Odoo_19/Dockerfile`, plus the declared EIE accommodation (see below) |
| `scripts/gen_env_odoo.sh` | Adds Odoo's own secret and the derived keyrings to the base rail env file |
| `scripts/odoo_driver.py` | Scenarios, run under `odoo shell` against the real registry |
| `scripts/run_odoo_e2e.sh` | Orchestrator: clean slate, boot, scenarios, isolation, logs, verdict |
| `scripts/assert_odoo_evidence.py` | Produces one verdict from one bundle. A missing mandatory check counts as FAIL. |
| `scripts/redact_odoo.py` | Redacts evidence and scans for leaks. Every env value is treated as a secret. |
| `results/` | Published run results (redacted), next to the base rail's `FINAL_E2E_REPORT.md` |

## Reproduce

```bash
dockerd &                                          # sandbox ships the CLI only
cp "$CA_BUNDLE" <gate>/tests/e2e/docker/ca/ca-bundle.crt    # only where TLS is intercepted
bash <gate>/tests/e2e/docker/scripts/build_images.sh all    # gate, eie, ceg
bash tests/e2e/constellation_odoo/scripts/build_images_odoo.sh all   # odoo (+ eie accommodation if needed)
bash <gate>/tests/e2e/docker/scripts/gen_env.sh /root/l9e2e/run.env
bash tests/e2e/constellation_odoo/scripts/run_odoo_e2e.sh
```

`<gate>` is `Constellation.Gate/constellation-gate`. All five repositories are
expected side by side under `L9_E2E_WORKSPACE` (default `/home/user`).
Evidence goes to `L9_E2E_RECEIPT_DIR` (default `/root/l9e2e/evidence-odoo/<UTC>/`).

## How Odoo is admitted by Gate

Odoo does **not** register with Gate. Registration (`POST /v1/admin/register`,
`X-Admin-Token`) is for workers that *execute* actions. A consumer is admitted
**only** because Gate can verify its packet signature: the key id `odoo-e2e` is
in Gate's `L9_VERIFYING_KEYS_JSON`. On the Odoo side:

| Where | What |
|---|---|
| `ir.config_parameter` | `plasticos.gate.url`, `local_node=odoo`, `org_id`, `signing_key_id=odoo-e2e`, `signing_algorithm=hmac-sha256`, `verify_response_signatures=1` |
| process env (never ICP) | `PLASTICOS_GATE_SIGNING_KEY` (Odoo's secret), `PLASTICOS_GATE_VERIFYING_KEYS_JSON` (`{"gate-e2e": …}`, so Odoo can verify Gate's replies) |

The worker nodes' keyrings are **not** widened. EIE and CEG only ever verify
Gate, which re-signs every hop.

## Scenarios

| ID | Kind | Proof |
|---|---|---|
| `O_INSTALL` | mandatory | `plasticos_gate`, `plasticos_enrichment`, `plasticos_matching` installed by Odoo 19 |
| `O_CONFIG` | mandatory | Odoo's own signing check passes, with no secret in ICP |
| `O_ISOLATION` | mandatory | From inside Odoo, only `gate` resolves |
| `O_T1_ROUNDTRIP` | mandatory | Odoo's `send_converge_action` gets a Gate-signed `response` (`source_node=gate`, `signing_key_id=gate-e2e`) carrying EIE's answer. EIE runs unchanged (staging, live provider, no provider egress). |
| `O_T2_OPERATOR_FAIL_CLOSED` | mandatory | The real **Execute** button on an EIE answer that is not `completed` fails closed: never `injected`, and no local fallback |
| `O_B1…B4` | mandatory | With EIE on the deterministic source: Execute leads to `review` with an allowlisted proposal (B1), the partner is untouched before approval (B2), **Inject** writes and blank-fills fields (B3), and a duplicate of one logical operation is served from Gate's idempotency cache (B4) |
| `O_N1` | mandatory | Odoo with signing removed is rejected by Gate |
| `O_N2` | mandatory | Any SDK install with a key id Gate doesn't know is rejected |
| `O_N3` | mandatory | Odoo's key id with the wrong secret is rejected |
| `O_N4` | mandatory | A consumer can't register itself as the owner of `converge` (no admin token) |
| `O_N5` | mandatory | Pointing the SDK at EIE directly can't work: the name doesn't resolve |
| `O_M1/M2` | match | `Odoo -> Gate -> CEG match`: M1 uses Odoo's own `MatchRequest` contract, M2 the direction CEG's spec declares |
| `O_G1…G4` | finding | What an **admitted** consumer key can also do (authorization gaps; reported, not gated) |

## Declared deviations

These are read back from image labels and the bundle, and printed in the verdict:

1. **CA layer** per stage, inherited from the base rail (transport only).
2. **EIE `sqlalchemy[asyncio]` layer.** This is applied only if the unmodified EIE image cannot
   `import sqlalchemy.ext.asyncio`. At EIE `b583c9e` it cannot: `pip install ".[dev]"` resolves
   the unpinned `sqlalchemy` to 2.1.x, which no longer pulls in `greenlet`, so the container exits
   before it registers. The layer is removed once EIE declares the extra.
3. **EIE `L9_ENVIRONMENT=test`** for the business phase only. The deterministic source is
   refused in staging, by design. Gate stays `staging` with mandatory signatures.
4. **`plasticos.gate.allow_insecure_http=1`.** Inside the Docker network Gate speaks plain HTTP.
   Integrity comes from HMAC packet signatures, not TLS.
