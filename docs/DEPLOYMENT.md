<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [deployment, infrastructure, hetzner]
owner: engine-team
status: active
/L9_META -->

# Deployment — L9 Graph Cognitive Engine

## Production Server

The Graph Cognitive Engine is deployed on Hetzner Cloud.

### Server Details

| Property | Value |
|----------|-------|
| **Provider** | Hetzner Cloud |
| **Server Name** | `l9-ceg` |
| **Server ID** | `#123423549` |
| **Plan** | CX33 |
| **IPv4** | `178.104.43.11` |
| **IPv6** | `2a01:4f8:1c19:15e4::/64` |
| **vCPU** | 4 (x86) |
| **RAM** | 8 GB |
| **Disk** | 80 GB Local |
| **Traffic** | 20 TB/month |
| **Cost** | $5.99/month |
| **Labels** | `managed_by: l9agent`, `role: ceg` |

### Network Endpoints

| Service | Endpoint | Port |
|---------|----------|------|
| **API** | `http://178.104.43.11:8000` | 8000 |
| **Health Check** | `http://178.104.43.11:8000/v1/health` | 8000 |
| **Neo4j Browser** | `http://178.104.43.11:7474` | 7474 |
| **Neo4j Bolt** | `bolt://178.104.43.11:7687` | 7687 |

### SSH Access

```bash
ssh root@178.104.43.11
```

### Deployment Path

```
/opt/ceg/
```

The repo is cloned directly on the server and containers are built locally.

### Environment Variables (Production)

`docker-compose.prod.yml` sets the fixed values itself (`L9_ENV=prod`, the SDK
chassis, `NEO4J_URI=bolt://neo4j:7687`, `DOMAINS_ROOT`, ...). Compose refuses to
start until these are set in `/opt/ceg/.env` (never committed; keep the master
copies in the secret store):

```bash
# Generated per deployment (e.g. openssl rand -hex 32)
NEO4J_PASSWORD=<from-secrets>
API_SECRET_KEY=<from-secrets>
L9_SIGNING_KEY=<from-secrets>            # CEG's own packet-signing key; Gate verifies with it
POSTGRES_PASSWORD=<from-secrets>         # Postgres superuser; used only at first init
CEG_APP_DB_PASSWORD=<from-secrets>       # app role the API connects as; URL-safe (hex)
# From Constellation.Gate
GATE_URL=<gate base URL>
GATE_ADMIN_TOKEN=<from-secrets>
L9_GATE_SIGNING_KEY_IDS=<gate ed25519 key id(s), comma-separated>
L9_VERIFYING_KEYS_JSON={"<gate key id>":"<gate verifying key>"}
# Operator-owned domain pack root (absolute host path)
CEG_DOMAINS_DIR=/opt/ceg/domain-packs
```

**Domain packs are not in the image (#400).** Neither `Dockerfile` nor
`Dockerfile.prod` copies a pack directory. `docker-compose.prod.yml` mounts
`CEG_DOMAINS_DIR` read-only at `/app/domains` and sets `DOMAINS_ROOT` to that
path, so the engine serves only what the operator placed there. With an empty
directory the engine starts and serves no domain. The packs kept in this
repository are design and test material and are never a production source.

Which layout the engine reads from that directory depends on
`DOMAIN_PACK_RECEIPTS_ENABLED`, which production compose does **not** set
(default `False`):

| `DOMAIN_PACK_RECEIPTS_ENABLED` | Layout served | How it gets there |
|---|---|---|
| unset / `false` (today) | `<root>/<tenant>/spec.yaml` | placed by the operator |
| `true` | `<root>/<tenant>/current.json` + `revisions/<sha256>/` | `tools/publish_domain_pack.py` |

A root written by `tools/publish_domain_pack.py` is therefore **not served**
until receipt mode is turned on: the loader finds no `spec.yaml`, lists no
domain, and the service still reports healthy. Turning the flag on in
production is a separate operator authorization
(`docs/runbooks/domain-pack-publish.md`, "Runtime feature discipline"); with it
on, the folder-shaped `spec.yaml` layout is no longer read.

Optional, with safe defaults: `NEO4J_USERNAME` (`neo4j`), `L9_SIGNING_KEY_ID`
(`graph-engine-v1`), `POSTGRES_USER` (`l9_owner`), `POSTGRES_DB` (`l9_graph`),
`CEG_APP_DB_USER` (`ceg_app`), `CORS_ORIGINS` (`[]`).

### PostgreSQL (compliance audit store)

The `postgres` service (`l9-graph-postgres-prod`, no published port) holds the
compliance audit log (`packet_audit_log`) and the PacketStore tables
(`packet_store`, `lineage_graph`, `hop_trace`, `delegation_chain`). On first
start with an empty volume it applies `engine/packet/packet_store.sql`, then
`scripts/postgres-init/02_ceg_app_role.sh` creates the app role. The API's
`POSTGRES_DSN` connects as that role, a member of `l9_product`, which may only
`INSERT`/`SELECT` the audit log. The log stays append-only because the API never
connects as the superuser.

PacketStore is on (`PACKET_STORE_ENABLED=true`, `PACKET_STORE_DSN` = the same app
role): every request/response packet pair through Gate is stored with its
lineage, so GDPR erasure also covers packets. `OUTCOME_PERSISTENCE_ENABLED=true`
links recorded match outcomes to their match packets. A store failure is logged,
never a request failure. **No pruning runs:** `prune_expired_packets()` exists in
`packet_store.sql` but nothing schedules it, so the tables grow with traffic.
They hold full packet payloads, including any personal data, so treat this
database and its backups as sensitive. See `docs/FEATURE_GATES.md`.

## Deployment Commands

### Check Server Health

```bash
# API health
curl http://178.104.43.11:8000/v1/health

# Neo4j connectivity
curl http://178.104.43.11:7474

# SSH and check containers
ssh root@178.104.43.11 "docker ps"
```

### Deploy New Version

```bash
# SSH to server, pull latest, rebuild and restart
ssh root@178.104.43.11 "cd /opt/ceg && git pull origin main && docker compose -f docker-compose.prod.yml build && docker compose -f docker-compose.prod.yml up -d"
```

### View Logs

```bash
ssh root@178.104.43.11 "cd /opt/ceg && docker compose -f docker-compose.prod.yml logs -f api"
```

### Restart Services

```bash
ssh root@178.104.43.11 "cd /opt/ceg && docker compose -f docker-compose.prod.yml restart api"
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    l9-ceg (178.104.43.11)                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐  ┌─────────────┐                          │
│  │   API       │  │   Neo4j     │                          │
│  │  :8000      │  │  :7474/7687 │                          │
│  │             │  │             │  │             │         │
│  │ FastAPI +   │  │ Graph DB +  │  │ Cache +     │         │
│  │ Uvicorn     │  │ GDS Plugin  │  │ Sessions    │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Monitoring

- **Prometheus Metrics**: `http://178.104.43.11:8000/v1/metrics`
- **Health Check**: `http://178.104.43.11:8000/v1/health`

## Backup & Recovery

Neo4j data is stored in Docker volumes. Backup commands:

```bash
# Backup Neo4j
ssh root@178.104.43.11 "docker exec neo4j neo4j-admin database dump neo4j --to-path=/backups"

# Copy backup locally
scp root@178.104.43.11:/opt/l9-ceg/backups/neo4j.dump ./backups/
```

## Firewall Rules

Managed via Hetzner Cloud Firewall. Required ports:

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 22 | TCP | Restricted IPs | SSH |
| 8000 | TCP | 0.0.0.0/0 | API |
| 7474 | TCP | Restricted IPs | Neo4j Browser |
| 7687 | TCP | Restricted IPs | Neo4j Bolt |

## Related Documentation

- [Docker Compose (prod)](../docker-compose.prod.yml)
- [Dockerfile (prod)](../Dockerfile.prod)
