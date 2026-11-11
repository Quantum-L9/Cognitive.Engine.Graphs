"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [compliance, pii]
owner: engine-team
status: active
--- /L9_META ---

engine/compliance/pii.py
PII detection, masking, and GDPR data subject operations.
Integrates with PacketEnvelope.security.pii_fields.

Exports: PIIHandler
"""

from __future__ import annotations

import hashlib
import logging
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# GDPR packet erasure (packet_store.sql gdpr_erase_subject). The tenant GUC
# is transaction-local, the same statement PacketStore uses (RU-079).
_SET_TENANT_SQL = "SELECT set_config('app.current_tenant', $1, true)"
_GDPR_ERASE_SQL = "SELECT gdpr_erase_subject($1, $2, $3)"
# Dry-run scope of the packet step: the same rows gdpr_erase_subject deletes.
_GDPR_COUNT_SQL = "SELECT count(*) FROM packet_store WHERE data_subject_id = $1 AND actor_tenant = $2"


# ── PII Field Classification ──────────────────────────────


class PIICategory(StrEnum):
    """PII sensitivity categories."""

    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    ADDRESS = "address"
    DOB = "date_of_birth"
    FINANCIAL = "financial"
    IP_ADDRESS = "ip_address"
    CUSTOM = "custom"


class PIISensitivity(StrEnum):
    LOW = "low"  # business name, city
    MEDIUM = "medium"  # email, phone
    HIGH = "high"  # SSN, DOB, financial account
    CRITICAL = "critical"  # combined identity (name + SSN + DOB)


# ── Patterns ──────────────────────────────────────────────

_PII_PATTERNS: dict[PIICategory, re.Pattern[str]] = {
    PIICategory.EMAIL: re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    ),
    # The guards keep a 10-digit run from matching inside a longer alphanumeric
    # token — opaque identifiers such as `q_5c9382647927` (handle_match's
    # query_id) otherwise read as phone numbers and get redacted out of the
    # response. `\b` cannot be used here because a leading `+` or `(` is not a
    # word character, so it would break `+1 (555) 123-4567`. SSN and IP_ADDRESS
    # below rely on `\b` for the same reason, their patterns start with a digit.
    PIICategory.PHONE: re.compile(
        r"(?<![0-9A-Za-z_])(?:\+?1[\-\s.]?)?\(?\d{3}\)?[\-\s.]?\d{3}[\-\s.]?\d{4}(?![0-9A-Za-z_])",
    ),
    PIICategory.SSN: re.compile(
        r"\b\d{3}[\-\s]?\d{2}[\-\s]?\d{4}\b",
    ),
    PIICategory.IP_ADDRESS: re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    ),
}

# Known PII field names (case-insensitive substring match)
_PII_FIELD_HINTS: dict[str, tuple[PIICategory, PIISensitivity]] = {
    "email": (PIICategory.EMAIL, PIISensitivity.MEDIUM),
    "phone": (PIICategory.PHONE, PIISensitivity.MEDIUM),
    "ssn": (PIICategory.SSN, PIISensitivity.HIGH),
    "social_security": (PIICategory.SSN, PIISensitivity.HIGH),
    "date_of_birth": (PIICategory.DOB, PIISensitivity.HIGH),
    "dob": (PIICategory.DOB, PIISensitivity.HIGH),
    "first_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "last_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "full_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "contact_name": (PIICategory.NAME, PIISensitivity.MEDIUM),
    "street": (PIICategory.ADDRESS, PIISensitivity.MEDIUM),
    "address": (PIICategory.ADDRESS, PIISensitivity.MEDIUM),
    "zip_code": (PIICategory.ADDRESS, PIISensitivity.LOW),
    "postal_code": (PIICategory.ADDRESS, PIISensitivity.LOW),
    "account_number": (PIICategory.FINANCIAL, PIISensitivity.HIGH),
    "routing_number": (PIICategory.FINANCIAL, PIISensitivity.HIGH),
    "credit_score": (PIICategory.FINANCIAL, PIISensitivity.HIGH),
    "ip_address": (PIICategory.IP_ADDRESS, PIISensitivity.LOW),
}


class PIIDetection(BaseModel):
    """Result of PII detection on a single field."""

    field_path: str
    category: PIICategory
    sensitivity: PIISensitivity
    detected_by: str  # "field_name" | "pattern_match"


# ── Structured paths (RU-062) ─────────────────────────────
#
# A path is a tuple of dict keys (str) and list/tuple indexes (int). Its string
# form is `a.b[0].c` — identical to the legacy dotted form for dict-only
# payloads. Edits copy every container on the edited path, so the caller's
# object is never mutated.

PathPart = str | int

# RU-064 / DEC-22 (R3-DEC-01 recommended (a)): the fixed marker PII_HANDLING.md's
# `redact` mode prescribes. `encrypt` / `tokenize` have no implementation (no key
# sourcing, no token store) and fail closed to this marker.
REDACTION_MARKER = "[REDACTED]"
_IMPLEMENTED_HANDLING = frozenset({"hash", "redact"})
_PATH_TOKEN = re.compile(r"\[(\d+)\]|([^.\[\]]+)")
_DELETE = object()


def _recursive_enabled() -> bool:
    from engine.config.settings import settings

    return bool(settings.pii_redaction_recursive)


def format_path(path: tuple[PathPart, ...]) -> str:
    """Render a structured path: `key`, `.key`, `[index]`."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def parse_path(path: str) -> tuple[PathPart, ...]:
    """Parse `a.b[0].c` into ``("a", "b", 0, "c")``."""
    parts: list[PathPart] = []
    for index, key in _PATH_TOKEN.findall(path):
        parts.append(int(index) if index else key)
    return tuple(parts)


def _path_sort_key(path: tuple[PathPart, ...]) -> tuple[tuple[int, Any], ...]:
    """Total order over mixed str/int paths (ints compare numerically)."""
    return tuple((0, part) if isinstance(part, int) else (1, part) for part in path)


def _edit_at_path(node: Any, path: tuple[PathPart, ...], transform: Any) -> Any:
    """Return ``node`` with ``transform`` applied at ``path``, copying containers on the path.

    ``transform(value)`` returns the replacement, or ``_DELETE`` to remove the
    key / element. A path that does not resolve leaves the structure as is.
    """
    if not path:
        return transform(node)
    head, rest = path[0], path[1:]
    if isinstance(node, dict) and isinstance(head, str):
        if head not in node:
            return node
        new_value = _edit_at_path(node[head], rest, transform)
        copied = dict(node)
        if new_value is _DELETE:
            copied.pop(head)
        else:
            copied[head] = new_value
        return copied
    if isinstance(node, (list, tuple)) and isinstance(head, int):
        if head >= len(node):
            return node
        new_value = _edit_at_path(node[head], rest, transform)
        items = list(node)
        if new_value is _DELETE:
            del items[head]
        else:
            items[head] = new_value
        return tuple(items) if isinstance(node, tuple) else items
    return node


class PIIHandler:
    """
    PII detection, masking, and GDPR data subject operations.

    Integrates with PacketEnvelope.security.pii_fields tuple.

    Usage:
        pii = PIIHandler()

        # Detect PII in a payload
        detections = pii.detect_pii({"email": "a@b.com", "account_id": 42})
        # → [PIIDetection(field_path="email", category=EMAIL, ...)]

        # Get pii_fields tuple for PacketEnvelope
        pii_fields = pii.get_pii_field_paths(payload)
        # → ("email", "contact_name")

        # Mask PII in payload
        masked = pii.mask_fields(payload, fields=["email", "ssn"])
        # → {"email": "***@***.com", "ssn": "***-**-****"}

        # Redact (remove) PII fields entirely
        redacted = pii.redact(payload, fields=["ssn"])
        # → {"email": "a@b.com"}  (ssn removed)

        # GDPR right-to-erasure
        pii.erase_subject(data_subject_id="user-123", stores=[...])
    """

    def __init__(
        self,
        additional_pii_fields: dict[str, tuple[PIICategory, PIISensitivity]] | None = None,
        mask_char: str = "*",
        declared_fields: list[str] | None = None,
    ):
        # RU-064: domain-declared PII keys (compliance.pii.fields + candidatefields),
        # matched exactly, case-insensitively, at any depth (structured traversal).
        self._declared = frozenset(f.casefold() for f in declared_fields or [])
        self._field_hints = dict(_PII_FIELD_HINTS)
        if additional_pii_fields:
            self._field_hints.update(additional_pii_fields)
        self._mask_char = mask_char
        self._patterns = dict(_PII_PATTERNS)

    # ── Detection ──────────────────────────────────────────

    def detect_pii(
        self,
        payload: dict[str, Any],
        prefix: str = "",
    ) -> list[PIIDetection]:
        """
        Detect PII fields in a payload dict (recursive).
        Returns list of PIIDetection for every field containing PII.
        """
        if _recursive_enabled():
            base = parse_path(prefix) if prefix else ()
            return [d for _, d in self._detect_structured(payload, base)]
        detections: list[PIIDetection] = []
        for key, value in payload.items():
            field_path = f"{prefix}.{key}" if prefix else key

            # Recurse into nested dicts
            if isinstance(value, dict):
                detections.extend(self.detect_pii(value, prefix=field_path))
                continue

            # Check field name hints
            key_lower = key.lower()
            for hint, (category, sensitivity) in self._field_hints.items():
                if hint in key_lower:
                    detections.append(
                        PIIDetection(
                            field_path=field_path,
                            category=category,
                            sensitivity=sensitivity,
                            detected_by="field_name",
                        )
                    )
                    break
            else:
                # Check value patterns (strings only)
                if isinstance(value, str):
                    for value_category, pattern in self._patterns.items():
                        if pattern.search(value):
                            sensitivity = _PII_FIELD_HINTS.get(
                                value_category.value,
                                (value_category, PIISensitivity.MEDIUM),
                            )[1]
                            detections.append(
                                PIIDetection(
                                    field_path=field_path,
                                    category=value_category,
                                    sensitivity=sensitivity,
                                    detected_by="pattern_match",
                                )
                            )
                            break

        return detections

    def _detect_structured(
        self,
        node: Any,
        path: tuple[PathPart, ...],
    ) -> list[tuple[tuple[PathPart, ...], PIIDetection]]:
        """RU-062: detection over dicts and lists/tuples with index-aware paths."""
        found: list[tuple[tuple[PathPart, ...], PIIDetection]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                key_path = (*path, str(key))
                if self._declared and str(key).casefold() in self._declared:
                    found.append(
                        (key_path, self._detection(key_path, PIICategory.CUSTOM, PIISensitivity.HIGH, "declared_field"))
                    )
                    continue
                if isinstance(value, dict):
                    found.extend(self._detect_structured(value, key_path))
                    continue
                hint = self._hint_for(str(key))
                if hint is not None:
                    found.append((key_path, self._detection(key_path, *hint, "field_name")))
                elif isinstance(value, (list, tuple)):
                    found.extend(self._detect_structured(value, key_path))
                else:
                    found.extend(self._detect_value(value, key_path))
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                item_path = (*path, index)
                if isinstance(item, (dict, list, tuple)):
                    found.extend(self._detect_structured(item, item_path))
                else:
                    found.extend(self._detect_value(item, item_path))
        return found

    def _hint_for(self, key: str) -> tuple[PIICategory, PIISensitivity] | None:
        key_lower = key.lower()
        for hint, category_sensitivity in self._field_hints.items():
            if hint in key_lower:
                return category_sensitivity
        return None

    def _detect_value(
        self,
        value: Any,
        path: tuple[PathPart, ...],
    ) -> list[tuple[tuple[PathPart, ...], PIIDetection]]:
        if not isinstance(value, str):
            return []
        for value_category, pattern in self._patterns.items():
            if pattern.search(value):
                sensitivity = _PII_FIELD_HINTS.get(
                    value_category.value,
                    (value_category, PIISensitivity.MEDIUM),
                )[1]
                return [(path, self._detection(path, value_category, sensitivity, "pattern_match"))]
        return []

    @staticmethod
    def _detection(
        path: tuple[PathPart, ...],
        category: PIICategory,
        sensitivity: PIISensitivity,
        detected_by: str,
    ) -> PIIDetection:
        return PIIDetection(
            field_path=format_path(path),
            category=category,
            sensitivity=sensitivity,
            detected_by=detected_by,
        )

    def detect_pii_paths(self, payload: Any) -> list[tuple[tuple[PathPart, ...], PIIDetection]]:
        """Structured detections ``(path, detection)`` over dicts and lists (RU-062)."""
        return self._detect_structured(payload, ())

    def get_pii_field_paths(self, payload: dict[str, Any]) -> tuple[str, ...]:
        """
        Return tuple of field paths containing PII.
        For PacketEnvelope.security.pii_fields.
        """
        detections = self.detect_pii(payload)
        return tuple(d.field_path for d in detections)

    # ── Masking ────────────────────────────────────────────

    def mask_fields(
        self,
        payload: dict[str, Any],
        fields: list[str] | None = None,
        mask_all_detected: bool = False,
    ) -> dict[str, Any]:
        """
        Mask PII fields in payload. Returns a new dict (does not mutate input).

        Args:
            payload: Input data
            fields: Explicit field paths to mask. If None and mask_all_detected,
                     auto-detects and masks all PII.
            mask_all_detected: If True, detect and mask all PII fields.
        """
        if _recursive_enabled():
            return self._mask_structured(payload, fields, mask_all_detected)

        result = dict(payload)  # shallow copy

        # Always detect PII to get category information
        detections = self.detect_pii(payload)
        category_map: dict[str, PIICategory] = {d.field_path: d.category for d in detections}

        if fields is None and mask_all_detected:
            fields = [d.field_path for d in detections]

        if not fields:
            return result

        for field_path in fields:
            category = category_map.get(field_path)
            self._mask_field_at_path(result, field_path.split("."), category=category)

        return result

    def _mask_structured(
        self,
        payload: dict[str, Any],
        fields: list[str] | None,
        mask_all_detected: bool,
    ) -> dict[str, Any]:
        """RU-062: copy-on-write masking that reaches list elements."""
        detections = self.detect_pii_paths(payload)
        category_map: dict[tuple[PathPart, ...], PIICategory] = {path: d.category for path, d in detections}
        if fields is None and mask_all_detected:
            paths = [path for path, _ in detections]
        else:
            paths = [parse_path(f) for f in fields or []]
        result: dict[str, Any] = dict(payload)
        for path in filter(None, paths):
            category = category_map.get(path)
            result = _edit_at_path(result, path, lambda v, c=category: self._mask_value(v, category=c))
        return result

    def _mask_field_at_path(
        self,
        data: dict[str, Any],
        path_parts: list[str],
        category: PIICategory | None = None,
    ) -> None:
        """Mask a field at a nested path in-place."""
        if len(path_parts) == 1:
            key = path_parts[0]
            if key in data:
                data[key] = self._mask_value(data[key], category=category)
            return

        key = path_parts[0]
        if key in data and isinstance(data[key], dict):
            self._mask_field_at_path(data[key], path_parts[1:], category=category)

    def _mask_value(self, value: Any, category: PIICategory | None = None) -> str:
        """Mask a single value based on its PII category."""
        s = str(value)

        # Email: show domain hint
        if "@" in s or category == PIICategory.EMAIL:
            if "@" in s:
                parts = s.split("@")
                domain_hint = parts[-1].split(".")[-1] if "." in parts[-1] else "com"
                return f"{self._mask_char * 3}@{self._mask_char * 3}.{domain_hint}"

        # SSN: mask all but last 4 (only when explicitly detected as SSN)
        if category == PIICategory.SSN:
            clean = s.replace("-", "").replace(" ", "")
            if len(clean) >= 4:
                return f"{self._mask_char * (len(clean) - 4)}{clean[-4:]}"

        # Financial account: mask all but last 4 (only when explicitly detected)
        if category == PIICategory.FINANCIAL:
            clean = s.replace("-", "").replace(" ", "")
            if clean.isdigit() and len(clean) >= 4:
                return f"{self._mask_char * (len(clean) - 4)}{clean[-4:]}"

        # Generic mask
        if len(s) <= 3:
            return self._mask_char * len(s)
        return f"{s[0]}{self._mask_char * (len(s) - 2)}{s[-1]}"

    # ── Redaction ──────────────────────────────────────────

    def redact(
        self,
        payload: dict[str, Any],
        fields: list[str] | None = None,
        redact_all_detected: bool = False,
    ) -> dict[str, Any]:
        """
        Remove PII fields entirely from payload. Returns a new dict.
        """
        if _recursive_enabled():
            if fields is None and redact_all_detected:
                paths = [path for path, _ in self.detect_pii_paths(payload)]
            else:
                paths = [parse_path(f) for f in fields or []]
            return self.redact_paths(payload, paths)

        result = dict(payload)

        if fields is None and redact_all_detected:
            detections = self.detect_pii(payload)
            fields = [d.field_path for d in detections]

        if not fields:
            return result

        for field_path in fields:
            self._redact_field_at_path(result, field_path.split("."))

        return result

    @staticmethod
    def redact_paths(payload: dict[str, Any], paths: list[tuple[PathPart, ...]]) -> dict[str, Any]:
        """Remove every structured path; the input is not mutated (RU-062).

        Paths are applied deepest/highest-index first so removing a list element
        never shifts the index of another path still to be applied.
        """
        result: dict[str, Any] = dict(payload)
        for path in sorted(set(filter(None, paths)), key=_path_sort_key, reverse=True):
            result = _edit_at_path(result, path, lambda _v: _DELETE)
        return result

    def _redact_field_at_path(self, data: dict[str, Any], path_parts: list[str]) -> None:
        """Remove field at nested path."""
        if len(path_parts) == 1:
            data.pop(path_parts[0], None)
            return
        key = path_parts[0]
        if key in data and isinstance(data[key], dict):
            self._redact_field_at_path(data[key], path_parts[1:])

    def apply_handling(self, payload: dict[str, Any], handling: str) -> dict[str, Any]:
        """Transform every detected PII field per a declared ``PIISpec.handling`` (RU-064).

        ``hash`` -> ``hash_value`` SHA-256 hex digest of the value's string form;
        ``redact`` -> ``REDACTION_MARKER``; ``encrypt`` / ``tokenize`` / anything
        else fail closed to ``REDACTION_MARKER`` with a logged handling decision.
        Copy-on-write: the input is never mutated. Logs field names only.
        """
        detections = self.detect_pii_paths(payload)
        if not detections:
            return dict(payload)
        mode = handling.casefold() if isinstance(handling, str) else ""
        if mode not in _IMPLEMENTED_HANDLING:
            logger.warning(
                "PII handling '%s' is not implemented; applying the redact marker to %d field(s): %s",
                handling,
                len(detections),
                [d.field_path for _, d in detections],
            )
            mode = "redact"

        def transform(value: Any) -> Any:
            if mode == "hash":
                return self.hash_value(str(value))
            return REDACTION_MARKER

        result: dict[str, Any] = dict(payload)
        for path, _ in detections:
            if path:
                result = _edit_at_path(result, path, transform)
        logger.debug("pii_fields_handled handling=%s fields=%d", mode, len(detections))
        return result

    # ── Hashing (for pseudonymization) ─────────────────────

    @staticmethod
    def hash_value(value: str, salt: str = "") -> str:
        """SHA-256 hash for pseudonymization. One-way, deterministic."""
        return hashlib.sha256(f"{salt}{value}".encode()).hexdigest()

    # ── GDPR Right-to-Erasure ──────────────────────────────

    async def count_subject_packets(self, data_subject_id: str, db_pool: Any, *, tenant: str | None) -> int:
        """Dry-run count of the packets ``erase_subject`` would delete.

        Same scope as ``gdpr_erase_subject``: the requesting tenant's rows for
        the subject id, read in one transaction with the transaction-local
        ``app.current_tenant`` set (while
        ``packet_store_rls_tenant_session_enabled``), so the RLS-enforced
        ``l9_product`` role counts the rows it would erase instead of 0.
        """
        if not tenant:
            msg = "GDPR packet erasure requires the requesting tenant"
            raise ValueError(msg)
        from engine.config.settings import settings

        async with db_pool.acquire() as conn, conn.transaction():
            if settings.packet_store_rls_tenant_session_enabled:
                await conn.fetchval(_SET_TENANT_SQL, tenant)
            count = await conn.fetchval(_GDPR_COUNT_SQL, data_subject_id, tenant)
        return int(count or 0)

    async def erase_subject(
        self,
        data_subject_id: str,
        graph_driver: Any = None,
        db_pool: Any = None,
        *,
        database: str | None = None,
        subject_property: str = "data_subject_id",
        performed_by: str = "system",
        tenant: str | None = None,
    ) -> dict[str, Any]:
        """
        GDPR right-to-erasure: delete all data for a data subject.

        Steps (the caller records the audit entry BEFORE calling this):
        1. Delete the requesting tenant's rows from PostgreSQL packet_store via
           ``gdpr_erase_subject($1, $2, $3)`` (subject id, performed_by,
           tenant) — the function also writes its own packet_audit_log rows
           before deleting
        2. Delete from Neo4j WHERE entity.<subject_property> = $dsid
        3. Delete from memory_embeddings via FK cascade
        4. Return summary of what was deleted

        ``subject_property`` is the domain's ``compliance.gdpr_subject_id_field``
        (RU-066); it is interpolated only after ``sanitize_label``, so an invalid
        name raises ``ValueError`` before any query. ``db_pool`` is the
        PacketStore pool; when it is ``None`` packets cannot be erased and the
        summary says so (``packets_deleted: None``, ``packets_erasable: False``)
        rather than reporting 0.

        ``tenant`` scopes the packet erasure (RU-066 follow-up, PR #352
        review): a subject id is not unique across tenants, so only rows whose
        ``actor_tenant`` is ``tenant`` are erased, and a packet erasure without
        a tenant raises ``ValueError`` before any query. The call runs in one
        transaction with the transaction-local ``app.current_tenant`` set to
        ``tenant`` (while ``packet_store_rls_tenant_session_enabled``, as
        PacketStore does since RU-079), so it also works on the RLS-enforced
        ``l9_product`` role, whose policies otherwise hide every row.

        ``database`` is the tenant's Neo4j database (tenant id == domain spec
        id == database name). DB-STRICT-01: ``GraphDriver.execute_query`` no
        longer falls back to the shared ``neo4j`` database when it is omitted,
        so the caller names the subject's domain database explicitly — the same
        ``domain_id`` the dry-run count in ``handle_admin`` already uses.
        """
        from engine.utils.security import sanitize_label

        prop = sanitize_label(subject_property)
        summary: dict[str, Any] = {
            "data_subject_id": data_subject_id,
            "packets_deleted": None,
            "packets_erasable": db_pool is not None,
            "graph_nodes_deleted": 0,
            "embeddings_deleted": 0,
        }

        if db_pool:
            if not tenant:
                msg = "GDPR packet erasure requires the requesting tenant"
                raise ValueError(msg)
            from engine.config.settings import settings

            try:
                async with db_pool.acquire() as conn, conn.transaction():
                    if settings.packet_store_rls_tenant_session_enabled:
                        await conn.fetchval(_SET_TENANT_SQL, tenant)
                    deleted = await conn.fetchval(_GDPR_ERASE_SQL, data_subject_id, performed_by, tenant)
                    summary["packets_deleted"] = int(deleted or 0)
            except Exception:
                logger.exception("GDPR erasure packet-store error")
                raise

        if graph_driver:
            try:
                result = await graph_driver.execute_query(
                    f"MATCH (n {{{prop}: $dsid}}) DETACH DELETE n RETURN count(n) AS deleted",
                    parameters={"dsid": data_subject_id},
                    database=database,
                )
                if result:
                    summary["graph_nodes_deleted"] = result[0].get("deleted", 0)
            except Exception:
                logger.exception("GDPR erasure graph error")
                raise

        logger.info(
            "GDPR erasure complete: subject_property=%s packets_deleted=%s graph_nodes_deleted=%s",
            prop,
            summary["packets_deleted"],
            summary["graph_nodes_deleted"],
        )
        return summary
