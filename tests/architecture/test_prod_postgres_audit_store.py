"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, architecture, docker, prod, postgres, audit]
owner: engine-team
status: active
--- /L9_META ---

tests/architecture/test_prod_postgres_audit_store.py
Production compose provisions the compliance audit store (W4-04) without
weakening it: Postgres is internal-only, the API connects as the l9_product app
role (never the superuser, which bypasses RLS and the grants that keep
packet_audit_log append-only), and every secret is required, not defaulted.

Static guards over docker-compose.prod.yml and scripts/postgres-init/. The
behavioural proof (the real service initialises, CEG's AuditLogger flushes as
the app role, and UPDATE/DELETE/TRUNCATE/DROP on the audit log are refused) needs
Docker and lives in the PR that introduced this; these guards keep that shape
from drifting.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"
ROLE_SCRIPT = ROOT / "scripts" / "postgres-init" / "02_ceg_app_role.sh"
SCHEMA = ROOT / "engine" / "packet" / "packet_store.sql"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_postgres_service_is_pinned_and_internal_only(compose: dict[str, Any]) -> None:
    postgres = compose["services"]["postgres"]
    assert re.fullmatch(r"postgres:16[\w.-]*@sha256:[0-9a-f]{64}", postgres["image"]), postgres["image"]
    assert "ports" not in postgres, "the audit store must not publish a port; reach it only on the compose network"
    assert "l9-net" in postgres["networks"]
    assert "postgres-data" in compose["volumes"]


def test_postgres_secrets_are_required_not_defaulted(compose: dict[str, Any]) -> None:
    env = compose["services"]["postgres"]["environment"]
    for name in ("POSTGRES_PASSWORD", "CEG_APP_DB_PASSWORD"):
        assert re.fullmatch(rf"\$\{{{name}:\?.+\}}", env[name]), f"{name} must be required (no default) in prod"


def test_schema_then_app_role_initialise_the_store(compose: dict[str, Any]) -> None:
    mounts = compose["services"]["postgres"]["volumes"]
    init = sorted(m.split(":")[1] for m in mounts if "/docker-entrypoint-initdb.d/" in m)
    assert init == [
        "/docker-entrypoint-initdb.d/01_packet_store.sql",
        "/docker-entrypoint-initdb.d/02_ceg_app_role.sh",
    ], "schema must apply before the role that is granted its l9_product privileges"
    assert "./engine/packet/packet_store.sql:/docker-entrypoint-initdb.d/01_packet_store.sql:ro" in mounts
    assert "./scripts/postgres-init/02_ceg_app_role.sh:/docker-entrypoint-initdb.d/02_ceg_app_role.sh:ro" in mounts
    assert SCHEMA.is_file()
    assert ROLE_SCRIPT.is_file()


def test_api_flushes_audit_as_the_app_role_after_postgres_is_healthy(compose: dict[str, Any]) -> None:
    api = compose["services"]["api"]
    dsn = api["environment"]["POSTGRES_DSN"]
    assert dsn.startswith("postgresql://${CEG_APP_DB_USER:-ceg_app}:${CEG_APP_DB_PASSWORD:?"), dsn
    assert "@postgres:5432/" in dsn
    assert "POSTGRES_USER" not in dsn, "the API must not connect as the superuser"
    assert "POSTGRES_PASSWORD" not in dsn, "the API must not hold the superuser password"
    assert api["depends_on"]["postgres"] == {"condition": "service_healthy"}


def test_app_role_is_least_privilege_l9_product_member() -> None:
    script = ROLE_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"ALTER ROLE :\"app_user\" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS", script)
    assert 'GRANT l9_product TO :"app_user";' in script
    assert "ON_ERROR_STOP=1" in script


def test_l9_product_keeps_the_audit_log_append_only() -> None:
    schema = SCHEMA.read_text(encoding="utf-8")
    assert "GRANT INSERT, SELECT ON packet_audit_log TO l9_product;" in schema
    grants = re.findall(r"GRANT ([^;]+) ON packet_audit_log TO l9_product;", schema)
    assert all("UPDATE" not in g and "DELETE" not in g and "TRUNCATE" not in g for g in grants), grants
