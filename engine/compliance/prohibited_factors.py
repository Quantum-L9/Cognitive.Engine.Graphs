"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [compliance, prohibited-factors]
owner: engine-team
status: active
--- /L9_META ---

Prohibited factor enforcement.
Blocks protected attributes at compile-time (ECOA, HIPAA, etc.).

RU-061 / CEG-COR-008: with ``settings.prohibited_factor_full_coverage`` on,
every property reference a gate, scoring dimension or sync row can carry into
Cypher is compared against the blocked list under casefold comparison. Off
restores the legacy check of ``GateSpec.candidateprop`` / ``queryparam`` only,
exact-case.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from engine.config.schema import DomainSpec, GateSpec

if TYPE_CHECKING:
    from engine.config.schema import ScoringDimensionSpec

logger = logging.getLogger(__name__)

# Every GateSpec field that names a candidate property or a query parameter.
# `queryparam` may also be a scalar literal (CEG-009b); only `str` values are
# field names, so non-str values are skipped.
_GATE_PROPERTY_FIELDS: tuple[str, ...] = (
    "candidateprop",
    "candidateprop_min",
    "candidateprop_max",
    "candidateprop_start",
    "candidateprop_end",
    "queryparam",
    "queryparam_min",
    "queryparam_max",
    "queryparam_start",
    "queryparam_end",
)

_DIMENSION_PROPERTY_FIELDS: tuple[str, ...] = ("candidateprop", "queryprop")


class ProhibitedFactorViolationError(ValueError):
    """A spec or payload references a blocked field.

    A ``ValueError`` (callers catching ``ValueError`` are unaffected) that also
    carries what was blocked, so ``ComplianceEngine`` can audit the violation
    (RU-063) without re-deriving it: ``subject_kind`` (gate | scoring_dimension
    | sync_row | sync_endpoint), ``subject`` (gate/dimension/endpoint name or
    the key path inside a sync row) and ``field`` (the blocked field name as
    written — never a value).
    """

    def __init__(self, message: str, *, subject_kind: str, subject: str, field: str) -> None:
        super().__init__(message)
        self.subject_kind = subject_kind
        self.subject = subject
        self.field = field


def _full_coverage() -> bool:
    from engine.config.settings import settings

    return bool(settings.prohibited_factor_full_coverage)


class ProhibitedFactorValidator:
    """Validates gates, scoring dimensions and sync rows don't use prohibited factors."""

    def __init__(self, domain_spec: DomainSpec):
        self.domain_spec = domain_spec
        self.blocked_fields: set[str] = set()

        if domain_spec.compliance and domain_spec.compliance.prohibitedfactors:
            pf_config = domain_spec.compliance.prohibitedfactors
            if pf_config.enabled:
                self.blocked_fields = set(pf_config.blockedfields)

    def is_blocked(self, name: object) -> bool:
        """True when ``name`` is a field name that matches a blocked field.

        Full coverage compares casefolded names; the legacy path is exact-case.
        Non-str values (literal operands) are never field names.
        """
        if not isinstance(name, str) or not name:
            return False
        if _full_coverage():
            # Derived from `blocked_fields` on each call: the set is a public
            # attribute callers may replace after construction.
            return name.casefold() in {field.casefold() for field in self.blocked_fields}
        return name in self.blocked_fields

    def validate_gate(self, gate_spec: GateSpec) -> None:
        """
        Validate gate doesn't reference prohibited fields.

        Args:
            gate_spec: Gate specification

        Raises:
            ValueError: If gate references prohibited field
        """
        if not self.blocked_fields:
            return  # No prohibited factors configured

        full = _full_coverage()
        fields = _GATE_PROPERTY_FIELDS if full else ("candidateprop", "queryparam")
        for attr in fields:
            value = getattr(gate_spec, attr, None)
            if self.is_blocked(value):
                via = f" (via {attr})" if full else ""
                msg = (
                    f"Gate '{gate_spec.name}' references prohibited field '{value}'{via}. "
                    f"Blocked fields: {self.blocked_fields}"
                )
                raise ProhibitedFactorViolationError(
                    msg, subject_kind="gate", subject=str(gate_spec.name), field=str(value)
                )

        logger.debug("Gate '%s' passed prohibited factor validation", gate_spec.name)

    def validate_scoring_dimension(self, dimension: ScoringDimensionSpec) -> None:
        """Validate a scoring dimension doesn't read a prohibited property.

        Dormant when blocking is disabled or the full-coverage flag is off.

        Raises:
            ValueError: If ``candidateprop`` or ``queryprop`` names a blocked field.
        """
        if not self.blocked_fields or not _full_coverage():
            return
        for attr in _DIMENSION_PROPERTY_FIELDS:
            value = getattr(dimension, attr, None)
            if self.is_blocked(value):
                msg = (
                    f"Scoring dimension '{dimension.name}' references prohibited field '{value}' "
                    f"(via {attr}). Blocked fields: {self.blocked_fields}"
                )
                raise ProhibitedFactorViolationError(
                    msg, subject_kind="scoring_dimension", subject=str(dimension.name), field=str(value)
                )

    def validate_sync_row(self, row: Any) -> None:
        """Validate every key at any depth of a sync batch row.

        Descends into dicts and lists/tuples of dicts (e.g. ``childsync``
        collections). The legacy path (flag off) checks top-level keys of a
        dict row only, exact-case.

        Raises:
            ValueError: If any key names a blocked field. The message names the
                key and its path, never a value.
        """
        if not self.blocked_fields:
            return
        if not _full_coverage():
            if isinstance(row, dict):
                for field_name in row:
                    if field_name in self.blocked_fields:
                        msg = (
                            f"Sync batch contains prohibited field '{field_name}'. "
                            f"Blocked fields: {self.blocked_fields}"
                        )
                        raise ProhibitedFactorViolationError(
                            msg, subject_kind="sync_row", subject=str(field_name), field=str(field_name)
                        )
            return
        self._scan_row(row, "")

    def _scan_row(self, node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_path = f"{path}.{key}" if path else str(key)
                if self.is_blocked(key):
                    msg = (
                        f"Sync batch contains prohibited field '{key}' at '{key_path}'. "
                        f"Blocked fields: {self.blocked_fields}"
                    )
                    raise ProhibitedFactorViolationError(msg, subject_kind="sync_row", subject=key_path, field=str(key))
                self._scan_row(value, key_path)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                self._scan_row(item, f"{path}[{index}]")
