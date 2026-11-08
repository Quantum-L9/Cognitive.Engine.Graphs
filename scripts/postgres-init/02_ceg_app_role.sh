#!/bin/sh
# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [docker]
# tags: [docker, prod, postgres, audit]
# owner: platform
# status: active
# --- /L9_META ---
#
# Postgres first-init step 2 (after 01_packet_store.sql): the CEG app login role.
#
# Mounted into /docker-entrypoint-initdb.d by docker-compose.prod.yml. The
# postgres image runs it once, on an empty data volume, as POSTGRES_USER over
# the local socket.
#
# CEG connects as this role (POSTGRES_DSN), a member of l9_product, and never
# as the superuser: a superuser bypasses row-level security and the grants
# that keep packet_audit_log append-only (l9_product: INSERT/SELECT only).
set -eu
: "${CEG_APP_DB_PASSWORD:?CEG_APP_DB_PASSWORD must be set}"
APP_USER="${CEG_APP_DB_USER:-ceg_app}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_user="$APP_USER" -v app_password="$CEG_APP_DB_PASSWORD" <<'EOSQL'
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') AS create_role \gset
\if :create_role
CREATE ROLE :"app_user" LOGIN;
\endif
ALTER ROLE :"app_user" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'app_password';
GRANT l9_product TO :"app_user";
EOSQL
