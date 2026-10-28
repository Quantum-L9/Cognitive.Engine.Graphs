#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [scripts]
# tags: [L9_TEMPLATE, scripts, seed]
# owner: platform
# status: active
# --- /L9_META ---
# ============================================================================
# seed.sh — Seed Neo4j with sample data for a domain
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

set -a; source "$ROOT_DIR/.env" 2>/dev/null || true; set +a

DOMAIN="${1:?Usage: seed.sh <domain> [api_url]}"
API_URL="${2:-http://localhost:${API_PORT:-8000}}"
SEED_DIR="$ROOT_DIR/domains/${DOMAIN}/seeds"

if [[ ! -d "$SEED_DIR" ]]; then
  echo "⚠️  No seeds directory: ${SEED_DIR}"
  echo "   Create JSON seed files in domains/${DOMAIN}/seeds/"
  echo "   Example: domains/${DOMAIN}/seeds/suppliers.json"
  exit 1
fi

echo "🌱 Seeding ${DOMAIN} from ${SEED_DIR}"

# Each seed file is the batch (a JSON array of entities) for the entity type
# named by the file. It is posted as one `sync` action through the single
# ingress, POST /v1/execute, in the ExecuteRequest envelope
# (docs/contracts/HANDLER_PAYLOADS.md): {action, tenant, payload}. That
# envelope is served by the legacy chassis (L9_CHASSIS=legacy, dev/local/test);
# the default SDK chassis accepts only Gate-authored packets on /v1/execute.
sync_request() {
  python3 - "$DOMAIN" "$1" "$2" <<'PY'
import json
import sys

domain, entity, path = sys.argv[1:4]
with open(path, encoding="utf-8") as fh:
    batch = json.load(fh)
if not isinstance(batch, list):
    sys.exit(f"{path}: a seed file must hold a JSON array of entities")
print(json.dumps({"action": "sync", "tenant": domain, "payload": {"entity_type": entity, "batch": batch}}))
PY
}

TOTAL=0
FAILED=0
for SEED_FILE in "$SEED_DIR"/*.json; do
  [[ -f "$SEED_FILE" ]] || continue
  ENTITY=$(basename "$SEED_FILE" .json)
  TOTAL=$((TOTAL + 1))

  echo "  📦 Syncing ${ENTITY}..."
  if ! BODY=$(sync_request "$ENTITY" "$SEED_FILE"); then
    echo "  ❌ ${ENTITY} failed (invalid seed file)"
    FAILED=$((FAILED + 1))
    continue
  fi
  HTTP_CODE=$(printf '%s' "$BODY" | curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${API_URL}/v1/execute" \
    -H "Content-Type: application/json" \
    --data-binary @-) || HTTP_CODE="000"

  if [[ "$HTTP_CODE" = "200" ]] || [[ "$HTTP_CODE" = "201" ]]; then
    echo "  ✅ ${ENTITY} synced (${HTTP_CODE})"
  else
    echo "  ❌ ${ENTITY} failed (${HTTP_CODE})"
    FAILED=$((FAILED + 1))
  fi
done

echo ""
if [[ "$FAILED" -gt 0 ]]; then
  echo "🌱 Seeding FAILED for ${DOMAIN}: ${FAILED} of ${TOTAL} entity type(s) did not sync"
  exit 1
fi
echo "🌱 Seeding complete for ${DOMAIN}"
