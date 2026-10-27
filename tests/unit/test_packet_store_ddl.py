"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, packet-store, ddl, schema]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_packet_store_ddl.py
Text-level checks on engine/packet/packet_store.sql, the only schema SSOT for
the PacketStore and the compliance audit log (docs/FEATURE_GATES.md §7).

RG-106 (RU-080 / CEG-COR-096): the file is safe to re-apply. It drops no
table, every CREATE TABLE / CREATE INDEX is IF NOT EXISTS, every CREATE POLICY
is preceded by DROP POLICY IF EXISTS for the same policy, and the script runs
in one transaction so a failure leaves nothing half-applied. The fresh-database
object set (tables, indexes, policies, functions) is pinned so the idempotent
rewrite cannot silently drop or rename an object.

The apply-twice proof against a real PostgreSQL is
tests/integration/test_packet_store_pg.py (RG-107).
"""

from __future__ import annotations

import re
from pathlib import Path

_SQL_PATH = Path(__file__).resolve().parents[2] / "engine" / "packet" / "packet_store.sql"


def _statements_text() -> str:
    """The DDL with ``--`` comments removed (comments may mention DROP)."""
    lines = []
    for line in _SQL_PATH.read_text().splitlines():
        lines.append(line.split("--", 1)[0])
    return "\n".join(lines)


_SQL = _statements_text()

_EXPECTED_TABLES = {"packet_store", "lineage_graph", "hop_trace", "delegation_chain", "packet_audit_log"}
_EXPECTED_INDEXES = {
    "idx_ps_actor_tenant",
    "idx_ps_originator",
    "idx_ps_trace",
    "idx_ps_correlation",
    "idx_ps_source_node",
    "idx_ps_dest_node",
    "idx_ps_type_action",
    "idx_ps_created",
    "idx_ps_ttl",
    "idx_ps_root",
    "idx_ps_classification",
    "idx_ps_data_subject",
    "idx_ps_tags",
    "idx_ps_compliance",
    "idx_ps_envelope",
    "idx_ht_packet",
    "idx_dc_packet",
    "idx_pal_packet",
    "idx_pal_tenant",
    "idx_lg_child",
}
_EXPECTED_POLICIES = {
    ("tenant_isolation_select", "packet_store"),
    ("tenant_isolation_insert", "packet_store"),
    ("tenant_isolation_delete", "packet_store"),
    ("hop_tenant", "hop_trace"),
    ("deleg_tenant", "delegation_chain"),
    ("audit_tenant", "packet_audit_log"),
    ("audit_insert", "packet_audit_log"),
    ("lineage_tenant", "lineage_graph"),
}
_EXPECTED_FUNCTIONS = {
    "insert_packet_idempotent",
    "gdpr_erase_subject",
    "prune_expired_packets",
    "get_ancestry",
    "get_descendants",
}


class TestPacketStoreDdlIsReRunnable:
    """RG-106: re-applying the file must not destroy data or abort half-built."""

    def test_no_drop_table(self) -> None:
        assert not re.search(r"\bDROP\s+TABLE\b", _SQL, re.IGNORECASE), "packet_store.sql must not DROP TABLE"

    def test_every_create_table_is_if_not_exists(self) -> None:
        creates = re.findall(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)(\w+)", _SQL, re.IGNORECASE)
        assert creates == [], f"CREATE TABLE without IF NOT EXISTS: {creates}"

    def test_every_create_index_is_if_not_exists(self) -> None:
        creates = re.findall(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS\b)(\w+)", _SQL, re.IGNORECASE)
        assert creates == [], f"CREATE INDEX without IF NOT EXISTS: {creates}"

    def test_every_create_policy_is_preceded_by_drop_policy_if_exists(self) -> None:
        for match in re.finditer(r"\bCREATE\s+POLICY\s+(\w+)\s+ON\s+(\w+)", _SQL, re.IGNORECASE):
            name, table = match.group(1), match.group(2)
            drop = re.search(
                rf"\bDROP\s+POLICY\s+IF\s+EXISTS\s+{name}\s+ON\s+{table}\s*;",
                _SQL[: match.start()],
                re.IGNORECASE,
            )
            assert drop is not None, f"CREATE POLICY {name} ON {table} has no preceding DROP POLICY IF EXISTS"

    def test_script_is_one_transaction(self) -> None:
        statements = [s.strip() for s in re.split(r";\s*\n", _SQL) if s.strip()]
        assert statements[0].upper() == "BEGIN", f"first statement is {statements[0]!r}, expected BEGIN"
        assert statements[-1].rstrip(";").strip().upper() == "COMMIT", (
            f"last statement is {statements[-1]!r}, expected COMMIT"
        )


class TestPacketStoreDdlFreshSchemaUnchanged:
    """Preserved invariant: a fresh database still gets every object."""

    def test_tables(self) -> None:
        tables = set(re.findall(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", _SQL, re.IGNORECASE))
        assert tables == _EXPECTED_TABLES

    def test_indexes(self) -> None:
        indexes = set(re.findall(r"\bCREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", _SQL, re.IGNORECASE))
        assert indexes == _EXPECTED_INDEXES

    def test_policies(self) -> None:
        policies = set(re.findall(r"\bCREATE\s+POLICY\s+(\w+)\s+ON\s+(\w+)", _SQL, re.IGNORECASE))
        assert policies == _EXPECTED_POLICIES

    def test_functions(self) -> None:
        functions = set(re.findall(r"\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+(\w+)", _SQL, re.IGNORECASE))
        assert functions == _EXPECTED_FUNCTIONS

    def test_rls_enabled_on_every_table(self) -> None:
        enabled = set(re.findall(r"\bALTER\s+TABLE\s+(\w+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY", _SQL, re.IGNORECASE))
        assert enabled == _EXPECTED_TABLES

    def test_audit_log_stays_append_only(self) -> None:
        """packet_audit_log: SELECT + INSERT policies only; l9_product gets INSERT, SELECT."""
        audit_policies = re.findall(
            r"\bCREATE\s+POLICY\s+\w+\s+ON\s+packet_audit_log\s+FOR\s+(\w+)", _SQL, re.IGNORECASE
        )
        assert sorted(p.upper() for p in audit_policies) == ["INSERT", "SELECT"]
        grant = re.search(r"\bGRANT\s+([\w\s,()]+?)\s+ON\s+packet_audit_log\s+TO\s+l9_product", _SQL, re.IGNORECASE)
        assert grant is not None
        privileges = {p.strip().upper() for p in grant.group(1).split(",")}
        assert privileges == {"INSERT", "SELECT"}
