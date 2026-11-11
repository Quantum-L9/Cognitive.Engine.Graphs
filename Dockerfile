# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [docker]
# tags: [L9_TEMPLATE, docker, dev]
# owner: platform
# status: active
# --- /L9_META ---
# ─────────────────────────────────────────────────────────────
# L9 Graph Cognitive Engine — Multi-stage Dockerfile
# Stage 1: deps    — install Python packages
# Stage 2: runtime — slim image with only what we need
# ─────────────────────────────────────────────────────────────

# ── Stage 1: Dependencies ──────────────────────────────────
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS deps

# git is required to resolve the git+https constellation-node-sdk dependency
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# uv.lock is the single lock: install_locked.sh bootstraps a hash-pinned uv into
# this stage's interpreter and syncs the runtime profile into /opt/venv, which
# is all the runtime stage copies (uv itself stays behind).
COPY pyproject.toml uv.lock ./
COPY tools/deps/install_locked.sh tools/deps/uv-bootstrap.txt tools/deps/
RUN UV_PROJECT_ENVIRONMENT=/opt/venv bash tools/deps/install_locked.sh runtime

# ── Stage 2: Runtime ───────────────────────────────────────
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS runtime

WORKDIR /app

# Copy the locked environment from the deps stage. Its interpreter links to
# /usr/local/bin/python3.12, which this stage shares (same base digest).
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY engine/ /app/engine/
# Domain packs are an explicit build-time selection, not an implicit part of
# the core image (Stage 2 R08). The default bundles this repository's example
# packs (PlasticOS among them) so `make dev` and the E2E rails keep working;
# a consumer deploying its own authorized packs points DOMAIN_PACKS_SRC at
# them, and DOMAINS_ROOT at runtime selects what the engine serves. The engine
# boots with an empty pack directory — no named domain is required.
ARG DOMAIN_PACKS_SRC=domains
COPY ${DOMAIN_PACKS_SRC}/ /app/domains/
COPY chassis/ /app/chassis/

# Copy entrypoint
COPY scripts/entrypoint.sh /app/entrypoint.sh

# Make the entrypoint executable and create the non-root user in a single layer
RUN chmod +x /app/entrypoint.sh \
    && groupadd -r l9 && useradd -r -g l9 -d /app -s /sbin/nologin l9 \
    && chown -R l9:l9 /app
USER l9

EXPOSE 8000

# Health check
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/v1/health'); exit(0 if r.status_code==200 and r.json().get('ready', True) else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
