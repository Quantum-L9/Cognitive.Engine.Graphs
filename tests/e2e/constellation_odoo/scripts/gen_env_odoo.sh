#!/usr/bin/env bash
# Extend a Constellation rail env file (Gate's gen_env.sh output) with the Odoo
# consumer identity. Writes a NEW file; the base file is not modified.
#
#   gen_env_odoo.sh <base run.env> <out odoo.env>
#
# Secrets generated here:
#   L9E2E_ODOO_KEY          Odoo's own HMAC signing secret (key id odoo-e2e)
#   L9E2E_ODOO_PG_PASSWORD  Odoo's database password
# Derived maps (values are secrets, keys are key ids):
#   L9E2E_GATE_VERIFYING_KEYS_JSON       Gate keyring: gate/eie/ceg + odoo-e2e
#   L9E2E_ODOO_GATE_VERIFYING_KEYS_JSON  what Odoo needs to verify Gate: gate-e2e
set -Eeuo pipefail

BASE="${1:?base env file (from Gate gen_env.sh) required}"
OUT="${2:?output env file required}"

python3 - "$BASE" "$OUT" <<'PY'
import json, os, secrets, sys

base_path, out_path = sys.argv[1], sys.argv[2]
env = {}
for line in open(base_path):
    k, sep, v = line.strip().partition("=")
    if sep:
        env[k] = v.strip("'")

odoo_key = secrets.token_hex(32)
pg_pw = secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")
node_ring = json.loads(env["L9E2E_VERIFYING_KEYS_JSON"])
gate_ring = {**node_ring, "odoo-e2e": odoo_key}

extra = {
    "L9E2E_ODOO_KEY": odoo_key,
    "L9E2E_ODOO_PG_PASSWORD": pg_pw,
    "L9E2E_GATE_VERIFYING_KEYS_JSON": json.dumps(gate_ring),
    "L9E2E_ODOO_GATE_VERIFYING_KEYS_JSON": json.dumps({"gate-e2e": env["L9E2E_GATE_KEY"]}),
}
old_umask = os.umask(0o077)
try:
    with open(out_path, "w") as fh:
        for line in open(base_path):
            fh.write(line if line.endswith("\n") else line + "\n")
        for k, v in extra.items():
            fh.write(f"{k}={v}\n")
finally:
    os.umask(old_umask)
print(f"wrote {out_path}")
print("key_ids=gate-e2e,eie-e2e,ceg-e2e,odoo-e2e (distinct secrets per identity)")
print("secrets_printed=false")
PY
