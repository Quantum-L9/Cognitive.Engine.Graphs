#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [scripts]
# tags: [L9_TEMPLATE, scripts, gds]
# owner: platform
# status: active
# --- /L9_META ---
# ============================================================================
# gds-trigger.sh — Manually trigger GDS algorithm jobs
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

set -a; source "$ROOT_DIR/.env" 2>/dev/null || true; set +a

API_URL="${API_URL:-http://localhost:${API_PORT:-8000}}"
DOMAIN="${1:?Usage: gds-trigger.sh <domain> <job_name>}"
JOB="${2:?Usage: gds-trigger.sh <domain> <job_name>  (a gds_jobs name from the domain spec)}"

# No admin subaction triggers every job, so there is no 'all' mode (DEC-52):
# name the job. The request is the `admin` action, subaction `gds_trigger`, in
# the ExecuteRequest envelope on POST /v1/execute (served by the legacy
# chassis, L9_CHASSIS=legacy; the default SDK chassis accepts only
# Gate-authored packets there).
if [[ "$JOB" = "all" ]]; then
  echo "❌ 'all' is not supported: pass one gds_jobs name from the ${DOMAIN} domain spec" >&2
  exit 2
fi

echo "⚙️  Triggering GDS job ${JOB} for ${DOMAIN}"

BODY=$(python3 -c 'import json, sys; print(json.dumps({"action": "admin", "tenant": sys.argv[1], "payload": {"subaction": "gds_trigger", "domain_id": sys.argv[1], "job_name": sys.argv[2]}}))' "$DOMAIN" "$JOB")
RESPONSE=$(printf '%s' "$BODY" | curl -sf -X POST "${API_URL}/v1/execute" \
  -H "Content-Type: application/json" \
  --data-binary @-)
printf '%s' "$RESPONSE" | python3 -m json.tool
