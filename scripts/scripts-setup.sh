#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [scripts]
# tags: [L9_TEMPLATE, scripts, setup]
# owner: platform
# status: active
# --- /L9_META ---
# ============================================================================
# setup.sh — One-shot local development environment setup
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "🔧 L9 Engine — Local Setup"
echo "=========================="

# --- Check prerequisites ---
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 required"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "❌ docker required"; exit 1; }
command -v docker compose >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1 || { echo "❌ docker compose required"; exit 1; }

echo "✅ Prerequisites met"

# --- Create .env if missing ---
if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cat > "$ROOT_DIR/.env" << 'EOF'
# L9 Engine — Environment Variables
# Consistent across all L9 repos

# Project
L9_PROJECT=l9-engine
L9_ENV=dev

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=devpassword
NEO4J_DATABASE=neo4j

# Redis
REDIS_URL=redis://localhost:6379/0

# API
API_PORT=8000
API_WORKERS=2
DOMAINS_ROOT=./domains
LOG_LEVEL=DEBUG
GDS_ENABLED=true
CORS_ORIGINS=*

# Secrets (dev only — use SSM in prod)
API_SECRET_KEY=dev-secret-key-change-in-prod
EOF
  echo "✅ Created .env (edit as needed)"
else
  echo "⏭️  .env already exists"
fi

# --- Install Python dependencies ---
if [[ -f "$ROOT_DIR/requirements-full.lock" ]]; then
  echo "📦 Installing hash-locked development dependencies..."
  bash "$ROOT_DIR/tools/deps/install_locked.sh" full
else
  echo "⚠️  No requirements-full.lock found — skipping Python deps"
fi

# --- Pull Docker images ---
echo "🐳 Pulling Docker images..."
docker pull neo4j:5.15-enterprise@sha256:b8491b6dcf998f216079d4793dde4b7d81759d8c05792fa48787c810e9e21e52
docker pull redis:7-alpine@sha256:8da19c7399dca900c6822a1cf30f9d2285585e2c4b457ff4095b0f4500054d3f

# --- Create Docker network ---
docker network create l9-net 2>/dev/null || true

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  ./scripts/dev.sh    # Start local stack"
echo "  ./scripts/test.sh   # Run tests"
echo "  ./scripts/seed.sh   # Load sample data"
