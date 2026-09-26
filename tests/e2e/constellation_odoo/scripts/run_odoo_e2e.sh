#!/usr/bin/env bash
# Odoo -> Gate -> EIE -> Gate -> Odoo on the Constellation Docker rail.
#
#   bash tests/e2e/constellation_odoo/scripts/run_odoo_e2e.sh
#
# Prerequisites (see ../README.md):
#   * dockerd running; gate/eie/ceg images from Constellation.Gate's
#     build_images.sh; l9e2e/odoo:local from build_images_odoo.sh
#   * sibling checkouts under L9_E2E_WORKSPACE (default /home/user)
#
# Every run starts from zero (down -v) and writes a secret-free, timestamped
# evidence bundle. The verdict (assert_odoo_evidence.py) is non-zero on any
# mandatory check that failed OR did not run.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIL="$(dirname "$HERE")"
WORKSPACE="${L9_E2E_WORKSPACE:-/home/user}"
GATE_RAIL="${WORKSPACE}/Constellation.Gate/constellation-gate/tests/e2e/docker"
STATE_DIR="${L9_E2E_STATE_DIR:-/root/l9e2e}"
BASE_ENV="${STATE_DIR}/run.env"
ODOO_ENV="${STATE_DIR}/odoo.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EV="${L9_E2E_RECEIPT_DIR:-${STATE_DIR}/evidence-odoo}/${STAMP}"
mkdir -p "$EV/flows" "$EV/logs"
echo "evidence: $EV"

export L9E2E_ODOO_REPO="${WORKSPACE}/IB-Odoo_19"
export L9E2E_ODOO_SCRIPTS="${HERE}"
BASE_COMPOSE=(docker compose --env-file "$ODOO_ENV" -f "${GATE_RAIL}/compose.yml" -f "${RAIL}/compose.odoo.yml")
DET_COMPOSE=("${BASE_COMPOSE[@]}" -f "${RAIL}/compose.eie-deterministic.yml")

envval() { python3 - "$1" "$2" <<'PY'
import sys
for line in open(sys.argv[1]):
    k, _, v = line.strip().partition("=")
    if k == sys.argv[2]:
        print(v.strip("'"))
PY
}

# ── 0. inputs: source revisions of all five repositories ─────────────────────
python3 - "$WORKSPACE" "$EV/source_revisions.json" <<'PY'
import json, subprocess, sys
ws, out = sys.argv[1], sys.argv[2]
repos = {"gate": "Constellation.Gate", "eie": "Enrichment.Inference.Engine",
         "ceg": "Cognitive.Engine.Graphs", "odoo": "IB-Odoo_19", "sdk": "Gate_SDK"}
res = {}
for node, d in repos.items():
    p = f"{ws}/{d}"
    head = subprocess.run(["git", "-C", p, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    br = subprocess.run(["git", "-C", p, "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", p, "status", "--porcelain", "--untracked-files=normal"],
                           capture_output=True, text=True).stdout.splitlines()
    res[node] = {"repo": d, "head": head, "branch": br, "clean": not dirty, "dirty_paths": dirty[:20]}
json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: (v["head"][:9], v["clean"]) for k, v in res.items()}))
PY

# ── 1. credentials (outside the repo; never copied into the bundle) ──────────
[[ -f "$BASE_ENV" ]] || bash "${GATE_RAIL}/scripts/gen_env.sh" "$BASE_ENV"
bash "${HERE}/gen_env_odoo.sh" "$BASE_ENV" "$ODOO_ENV"

# ── 2. clean slate + redacted topology ───────────────────────────────────────
echo "== down -v =="
"${BASE_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${BASE_COMPOSE[@]}" config | python3 "${HERE}/redact_odoo.py" - "$EV/compose_config.yml" "$ODOO_ENV"

# ── 3. boot (Odoo installs its addons on first start) ────────────────────────
echo "== up =="
"${BASE_COMPOSE[@]}" up -d --wait --wait-timeout 900 || true
docker ps -a --format '{{.Names}} {{.Status}}' | grep l9e2e | tee "$EV/boot_state.txt"

NEO4J_PW="$(envval "$ODOO_ENV" L9E2E_NEO4J_PASSWORD)"
for _ in $(seq 1 30); do
  docker exec l9e2e-neo4j cypher-shell -u neo4j -p "$NEO4J_PW" -d system \
    "CREATE DATABASE plasticos IF NOT EXISTS WAIT;" >/dev/null 2>&1 && break
  sleep 5
done
docker restart l9e2e-ceg >/dev/null 2>&1 || true

await_registry() {  # $1 = expected node count
  for i in $(seq 1 60); do
    curl -sS --noproxy '*' -o "$EV/registry_probe.json" http://127.0.0.1:19000/v1/registry 2>/dev/null || true
    n="$(python3 -c 'import json,sys
try: print(sum(1 for v in json.load(open(sys.argv[1])).values() if v.get("healthy")))
except Exception: print(0)' "$EV/registry_probe.json")"
    [[ "$n" == "$1" ]] && { echo "registry: $n healthy nodes after ~$((i*5))s"; return 0; }
    sleep 5
  done
  echo "registry: expected $1 healthy nodes, have ${n:-0}"; return 1
}
await_registry 2 | tee "$EV/registry_wait.txt" || true

# EIE's image entrypoint runs `alembic upgrade head` on start; re-running it
# here is an idempotent check that the schema is at head (no-op when it is).
docker exec -w /app l9e2e-eie alembic upgrade head > "$EV/eie_migrations.txt" 2>&1 \
  || echo "EIE MIGRATION FAILED (see eie_migrations.txt)"
docker exec l9e2e-eie-pg psql -U enrich -d enrich -Atc \
  "select table_name from information_schema.tables where table_schema='public' order by 1" \
  >> "$EV/eie_migrations.txt" 2>&1 || true
curl -sS --noproxy '*' -o "$EV/registry_probe.json" http://127.0.0.1:19000/v1/registry || true
python3 -m json.tool "$EV/registry_probe.json" > "$EV/gate_registry.json" || true
rm -f "$EV/registry_probe.json"

# ── 4. what actually runs: image revisions, accommodations, SDK commits ──────
python3 - "$EV/image_provenance.json" <<'PY'
import json, subprocess, sys
probe = ("import json,glob,os\n"
         "o={}\n"
         "for d in sorted(glob.glob('/usr/local/lib/python3*/*-packages/constellation_node_sdk-*.dist-info')):\n"
         "    o['dist_info']=os.path.basename(d)\n"
         "    p=os.path.join(d,'direct_url.json')\n"
         "    o['direct_url']=json.load(open(p)) if os.path.exists(p) else None\n"
         "print(json.dumps(o))\n")
out = {}
for node in ("gate", "eie", "ceg", "odoo"):
    img = f"l9e2e/{node}:local"
    fmt = '{{json .Config.Labels}}'
    labels = subprocess.run(["docker", "image", "inspect", img, "--format", fmt], capture_output=True, text=True)
    run = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python3", img, "-c", probe],
                         capture_output=True, text=True)
    try:
        sdk = json.loads(run.stdout.strip() or "{}")
    except json.JSONDecodeError:
        sdk = {"error": (run.stderr or run.stdout)[-300:]}
    du = sdk.get("direct_url") or {}
    commit = (du.get("vcs_info") or {}).get("commit_id")
    if not commit and du.get("url", "").endswith(".tar.gz"):
        commit = du["url"].rsplit("/", 1)[-1][:-7]
    out[node] = {"labels": json.loads(labels.stdout or "null"), "sdk": sdk, "sdk_commit": commit}
json.dump(out, open(sys.argv[1], "w"), indent=1)
print(json.dumps({k: {"rev": (v["labels"] or {}).get("org.opencontainers.image.revision", "")[:9],
                      "accommodation": (v["labels"] or {}).get("io.l9.e2e.accommodation"),
                      "sdk": (v["sdk_commit"] or "")[:9]} for k, v in out.items()}, indent=1))
PY

# ── 5. Odoo scenarios, inside the real Odoo 19 registry ──────────────────────
odoo_phase() {  # $1 = phase
  local raw="$EV/flows/odoo_$1.raw.txt"
  docker exec -e L9E2E_PHASE="$1" l9e2e-odoo sh -c \
    'odoo shell -d l9e2e --db_host=odoo-db --db_port=5432 --db_user=odoo \
       --db_password="$L9E2E_ODOO_DB_PASSWORD" \
       --addons-path=/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons \
       --shell-interface=python --log-level=warn < /l9e2e/odoo_driver.py' \
    > "$raw.unredacted" 2>&1 || true
  python3 "${HERE}/redact_odoo.py" "$raw.unredacted" "$raw" "$ODOO_ENV"
  rm -f "$raw.unredacted"
  python3 - "$raw" "$EV/flows/odoo_$1.json" <<'PY'
import json, sys
text = open(sys.argv[1]).read()
try:
    body = text.split("L9E2E-RESULT-BEGIN", 1)[1].split("L9E2E-RESULT-END", 1)[0]
    data = json.loads(body)
except (IndexError, json.JSONDecodeError):
    data = {"driver_error": "no result block", "tail": text[-2000:]}
json.dump(data, open(sys.argv[2], "w"), indent=1)
for k, v in (data.get("checks") or {}).items():
    print(f"  {k}: {v.get('status')}")
if data.get("driver_error"):
    print("  DRIVER ERROR:", str(data["driver_error"])[-400:])
PY
}

echo "== odoo: configure ==";   odoo_phase configure
echo "== odoo: transport (EIE staging, live provider) =="; odoo_phase transport
echo "== odoo: match (Odoo -> Gate -> CEG) =="; odoo_phase match
echo "== odoo: adversarial ==";  odoo_phase adversarial

echo "== EIE -> deterministic source (business path) =="
"${DET_COMPOSE[@]}" up -d --wait --wait-timeout 300 enrichment-engine || true
await_registry 2 | tee "$EV/registry_wait_business.txt" || true
docker exec l9e2e-eie sh -c 'echo "L9_ENVIRONMENT=$L9_ENVIRONMENT L9_ENRICHMENT_PROVIDER=$L9_ENRICHMENT_PROVIDER"' \
  > "$EV/eie_business_profile.txt" 2>&1 || true
echo "== odoo: business ==";     odoo_phase business

# ── 5b. Gate restart: Gate's registry is in memory; both workers must
#        re-register on their own (EIE loop, CEG gate_reregistration_enabled).
echo "== Gate restart recovery =="
docker restart l9e2e-gate >/dev/null
python3 - "$EV/flows/gate_restart_recovery.json" <<'PY'
import json, sys, time, urllib.request
t0 = time.time(); seen = {}; nodes = []
while time.time() - t0 < 120:
    try:
        with urllib.request.urlopen("http://127.0.0.1:19000/v1/registry", timeout=4) as r:
            nodes = sorted(json.load(r))
    except Exception:
        nodes = []
    for n in nodes:
        seen.setdefault(n, round(time.time() - t0, 1))
    if {"enrichment-engine", "graph"} <= set(nodes):
        break
    time.sleep(2)
res = {"reregistered_after_s": seen, "registry": nodes,
       "verdict": "PASS" if {"enrichment-engine", "graph"} <= set(nodes) else "FAIL"}
json.dump(res, open(sys.argv[1], "w"), indent=1)
print("gate restart recovery:", res["verdict"], seen)
PY
echo "== odoo: match after Gate restart =="; odoo_phase match

# ── 6. structural isolation, asserted against Docker itself ─────────────────
python3 - "$EV/flows/isolation.json" <<'PY'
import json, subprocess, sys
def probe(c, h):
    r = subprocess.run(["docker", "exec", c, "getent", "hosts", h], capture_output=True, text=True)
    return {"container": c, "host": h, "exit_code": r.returncode}
res = {
    "odoo_to_eie_must_fail": probe("l9e2e-odoo", "enrichment-engine"),
    "odoo_to_ceg_must_fail": probe("l9e2e-odoo", "graph"),
    "odoo_to_gate_must_work": probe("l9e2e-odoo", "gate"),
    "eie_to_odoo_must_fail": probe("l9e2e-eie", "odoo"),
}
ok = (res["odoo_to_eie_must_fail"]["exit_code"] != 0 and res["odoo_to_ceg_must_fail"]["exit_code"] != 0
      and res["odoo_to_gate_must_work"]["exit_code"] == 0 and res["eie_to_odoo_must_fail"]["exit_code"] != 0)
res["verdict"] = "PASS" if ok else "FAIL"
json.dump(res, open(sys.argv[1], "w"), indent=1)
print("isolation:", res["verdict"])
PY

# ── 7. logs (redacted) + state ───────────────────────────────────────────────
for c in gate eie ceg odoo neo4j; do
  docker logs "l9e2e-$c" > "$EV/logs/$c.log.unredacted" 2>&1 || true
  python3 "${HERE}/redact_odoo.py" "$EV/logs/$c.log.unredacted" "$EV/logs/$c.log" "$ODOO_ENV"
  rm -f "$EV/logs/$c.log.unredacted"
done
docker ps -a --format '{{json .}}' | grep l9e2e > "$EV/container_state.json" || true

# ── 8. verdict ───────────────────────────────────────────────────────────────
python3 "${HERE}/redact_odoo.py" --scan "$EV" "$ODOO_ENV" > "$EV/secret_scan.json" || true
cat "$EV/secret_scan.json"
# `set -e` + pipefail would abort on a FAIL verdict before the bundle path is
# printed; capture the verdict's status explicitly instead.
status=0
python3 "${HERE}/assert_odoo_evidence.py" "$EV" > "$EV/assertions.txt" || status=$?
cat "$EV/assertions.txt"
echo "evidence bundle: $EV"
exit "$status"
