"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [compliance]
tags: [compliance, engine]
owner: engine-team
status: active
--- /L9_META ---

Compliance orchestrator.
Coordinates prohibited-factor validation, PII handling, and audit logging
across match, sync, and admin request lifecycles.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from engine.compliance.audit import AuditLogger
from engine.compliance.pii import PIICategory, PIIHandler, PIISensitivity
from engine.compliance.prohibited_factors import ProhibitedFactorValidator, ProhibitedFactorViolationError

if TYPE_CHECKING:
    from engine.config.schema import DomainSpec, GateSpec, ScoringDimensionSpec, SyncEndpointSpec

logger = logging.getLogger(__name__)


def _pii_recursive() -> bool:
    from engine.config.settings import settings

    return bool(settings.pii_redaction_recursive)


class ComplianceEngine:
    """Orchestrates all compliance checks for a domain."""

    def __init__(self, domain_spec: DomainSpec, db_pool: Any | None = None) -> None:
        self._spec = domain_spec
        self._db_pool = db_pool
        self._prohibited = ProhibitedFactorValidator(domain_spec)

        from engine.config.settings import settings

        compliance = getattr(domain_spec, "compliance", None)
        pii_cfg = getattr(compliance, "pii", None) if compliance else None
        # RU-064: the declared compliance.pii handling applies when the section is
        # present and enabled (and list traversal, RU-062, is on so declared
        # candidate fields inside `candidates[*]` are reachable).
        self._pii_handling: str | None = None
        if settings.pii_declared_fields_enforced and settings.pii_redaction_recursive:
            declared: list[str] = []
            if pii_cfg is not None and getattr(pii_cfg, "enabled", False) is True:
                declared = [*getattr(pii_cfg, "fields", []), *getattr(pii_cfg, "candidatefields", [])]
                self._pii_handling = str(getattr(pii_cfg, "handling", "hash"))
            self._pii = PIIHandler(declared_fields=declared)
        else:
            # Legacy: probe a `compliance.pii.additional_fields` attribute PIISpec never had.
            additional_pii: dict[str, tuple[PIICategory, PIISensitivity]] = {}
            if pii_cfg:
                if hasattr(pii_cfg, "additional_fields") and isinstance(pii_cfg.additional_fields, dict):
                    additional_pii = pii_cfg.additional_fields
            self._pii = PIIHandler(additional_pii_fields=additional_pii)
        if settings.compliance_audit_soft_sink:
            # RU-065: retention bounded by the documented W4-04 setting.
            self._audit = AuditLogger(buffer_size=settings.compliance_buffer_max)
        else:
            self._audit = AuditLogger()  # Uses default retention policies

        if self._db_pool is None:
            logger.info("compliance.audit_persistence_disabled", extra={"reason": "db_pool not configured"})

        # Default to enabled for SOC2/HIPAA compliance.
        # Only disable if domain_spec.compliance.enabled is explicitly False.
        self._enabled = True
        if domain_spec.compliance and hasattr(domain_spec.compliance, "enabled"):
            if domain_spec.compliance.enabled is False:
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def flush_audit(self) -> int:
        """Flush audit buffer to persistent storage.

        Returns:
            Number of entries persisted.
        """
        if self._db_pool is None:
            logger.warning("No db_pool configured, cannot flush audit entries")
            return 0
        return await self._audit.flush_to_store(self._db_pool)

    async def flush_audit_for_request(self) -> int:
        """Request-path flush (handle_match / handle_sync).

        RU-065: the audit pool is a soft dependency (boot.py). With
        ``compliance_audit_soft_sink`` on, a sink failure is logged and the
        request continues; the entries stay buffered (bounded) for the boot
        periodic flush loop. Off: the failure propagates as before.
        """
        from engine.config.settings import settings

        if not settings.compliance_audit_soft_sink:
            return await self.flush_audit()
        try:
            return await self.flush_audit()
        except Exception as exc:
            logger.warning(
                "Compliance audit flush failed on the request path (%s); %d entries remain buffered, %d dropped so far",
                type(exc).__name__,
                self._audit.buffer_count,
                self._audit.dropped_count,
            )
            return 0

    def _audit_violation(
        self,
        violation: ProhibitedFactorViolationError,
        *,
        tenant: str | None,
        resource_type: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """RU-063: record a denied attempt before the violation propagates.

        Active when ``prohibited_factor_violation_audit`` is on and the
        domain's ``prohibitedfactors.auditonviolation`` is true (schema default).
        The entry names the gate / dimension / endpoint / row path and the
        blocked field name, never a value.
        """
        from engine.config.settings import settings

        if not settings.prohibited_factor_violation_audit:
            return
        compliance = getattr(self._spec, "compliance", None)
        pf_config = getattr(compliance, "prohibitedfactors", None)
        if pf_config is None or not getattr(pf_config, "auditonviolation", True):
            return
        regimes = [str(r.name) for r in (getattr(compliance, "regimes", None) or []) if getattr(r, "name", None)]
        actor = tenant or "unknown"
        prefix = f"{resource_type}:" if resource_type else ""
        self._audit.log_violation(
            actor=actor,
            tenant=actor,
            resource=f"{violation.subject_kind}:{prefix}{violation.subject}:{violation.field}",
            blocked_field=violation.field,
            detail=f"Prohibited field '{violation.field}' referenced by {violation.subject_kind} '{violation.subject}'",
            resource_type=resource_type or violation.subject_kind,
            compliance_tags=regimes,
            trace_id=trace_id,
        )

    def validate_gates(self, gates: list[GateSpec], *, tenant: str | None = None) -> None:
        """Validate all gates against prohibited factors at compile time.

        Raises:
            ValueError: If any gate references a prohibited field (audited first, RU-063).
        """
        for gate in gates:
            try:
                self._prohibited.validate_gate(gate)
            except ProhibitedFactorViolationError as violation:
                self._audit_violation(violation, tenant=tenant)
                raise

    def validate_scoring(self, dimensions: list[ScoringDimensionSpec], *, tenant: str | None = None) -> None:
        """Validate scoring dimensions against prohibited factors (RU-061).

        Raises:
            ValueError: If any dimension's candidateprop/queryprop is a blocked field
                (audited first, RU-063).
        """
        for dimension in dimensions:
            try:
                self._prohibited.validate_scoring_dimension(dimension)
            except ProhibitedFactorViolationError as violation:
                self._audit_violation(violation, tenant=tenant)
                raise

    def validate_sync_fields(self, endpoint_spec: SyncEndpointSpec) -> None:
        """Validate sync endpoint field mappings against prohibited factors.

        Raises:
            ValueError: If endpoint maps to a prohibited field.
        """
        if not self._prohibited.blocked_fields:
            return

        # Check fieldsupdated list (PATCH operations)
        if endpoint_spec.fieldsupdated:
            for field in endpoint_spec.fieldsupdated:
                if self._prohibited.is_blocked(field):
                    msg = (
                        f"Sync endpoint '{endpoint_spec.path}' updates prohibited "
                        f"field '{field}'. Blocked fields: {self._prohibited.blocked_fields}"
                    )
                    raise ProhibitedFactorViolationError(
                        msg, subject_kind="sync_endpoint", subject=str(endpoint_spec.path), field=str(field)
                    )

        # Check idproperty
        if endpoint_spec.idproperty and self._prohibited.is_blocked(endpoint_spec.idproperty):
            msg = (
                f"Sync endpoint '{endpoint_spec.path}' uses prohibited "
                f"idproperty '{endpoint_spec.idproperty}'. Blocked fields: {self._prohibited.blocked_fields}"
            )
            raise ProhibitedFactorViolationError(
                msg,
                subject_kind="sync_endpoint",
                subject=str(endpoint_spec.path),
                field=str(endpoint_spec.idproperty),
            )

    def check_match_request(
        self,
        *,
        tenant: str,
        query: dict[str, Any],
        match_direction: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Run pre-match compliance checks. Returns sanitized query."""
        self._audit.log_query(
            actor=tenant,
            tenant=tenant,
            detail=f"Match request direction={match_direction}",
            trace_id=trace_id,
        )
        # Detect PII in query for audit logging — do NOT mask inbound queries.
        # Masking replaces values with '***', corrupting graph traversal results.
        # PII redaction is applied on the RESPONSE path via redact_response().
        pii_fields = self._pii.get_pii_field_paths(query)
        if pii_fields:
            logger.info(
                "PII detected in match query (fields: %s) — logged for audit, query passed through",
                [str(f) for f in pii_fields],
            )
            self._audit.log_access(
                actor=tenant,
                tenant=tenant,
                resource=", ".join(str(f) for f in pii_fields),
                resource_type="pii_field",
                pii_fields_accessed=[str(f) for f in pii_fields],
                trace_id=trace_id,
            )
        return query

    def check_sync_request(
        self,
        *,
        tenant: str,
        entity_type: str,
        batch: list[dict[str, Any]],
        trace_id: str | None = None,
        endpoint_spec: SyncEndpointSpec | None = None,
    ) -> None:
        """Audit sync operations and validate against prohibited factors."""
        # Validate endpoint spec if provided
        if endpoint_spec:
            try:
                self.validate_sync_fields(endpoint_spec)
            except ProhibitedFactorViolationError as violation:
                self._audit_violation(violation, tenant=tenant, resource_type=entity_type, trace_id=trace_id)
                raise

        # Check batch data for prohibited fields
        for item in batch:
            # RU-061: every key at any depth (childsync lists included), casefolded.
            try:
                self._prohibited.validate_sync_row(item)
            except ProhibitedFactorViolationError as violation:
                self._audit_violation(violation, tenant=tenant, resource_type=entity_type, trace_id=trace_id)
                raise

            # Detect and log PII access
            pii_fields = self._pii.get_pii_field_paths(item)
            if pii_fields:
                self._audit.log_access(
                    actor=tenant,
                    tenant=tenant,
                    resource=f"{entity_type}:{item.get('entity_id', 'unknown')}",
                    resource_type=entity_type,
                    trace_id=trace_id,
                    # RU-062: name the (index-aware) field paths, never values.
                    pii_fields_accessed=list(pii_fields) if _pii_recursive() else None,
                )

        # Log the sync operation
        self._audit.log_mutation(
            actor=tenant,
            tenant=tenant,
            resource=entity_type,
            detail=f"Sync {entity_type} batch_size={len(batch)}",
            trace_id=trace_id,
        )

    def redact_response(
        self,
        response: dict[str, Any],
        tenant: str,
    ) -> dict[str, Any]:
        """Redact PII from response before returning to client."""
        if not self._enabled:
            return response
        if self._pii_handling is not None:
            # RU-064: declared fields + built-in inference, transformed per the
            # domain's declared handling mode (hash / redact; encrypt and
            # tokenize fail closed to redact).
            return self._pii.apply_handling(response, self._pii_handling)
        if _pii_recursive():
            # RU-062: detection and removal reach list elements (match
            # `candidates[*].candidate`) and never mutate `response`.
            return self._pii.redact(response, redact_all_detected=True)
        # Detect and redact PII fields
        pii_fields = self._pii.get_pii_field_paths(response)
        if pii_fields:
            return self._pii.redact(response, list(pii_fields))
        return response

    def log_outcome(
        self,
        *,
        tenant: str,
        outcome_id: str,
        outcome: str,
        trace_id: str | None = None,
    ) -> None:
        """Audit outcome feedback."""
        self._audit.log_mutation(
            actor=tenant,
            tenant=tenant,
            resource=outcome_id,
            detail=f"Outcome recorded: {outcome_id} result={outcome}",
            trace_id=trace_id,
        )

    def log_pii_erasure(
        self,
        *,
        tenant: str,
        data_subject_id: str,
        detail: str,
        trace_id: str | None = None,
    ) -> None:
        """Audit a GDPR erasure in this domain's trail (RU-066). Emitted before deletion."""
        self._audit.log_pii_erasure(
            actor=tenant,
            tenant=tenant,
            data_subject_id=data_subject_id,
            detail=detail,
            trace_id=trace_id,
        )

    def log_admin(
        self,
        *,
        tenant: str,
        subaction: str,
        trace_id: str | None = None,
    ) -> None:
        """Audit admin operations."""
        self._audit.log_delegation(
            actor=tenant,
            tenant=tenant,
            resource="admin",
            detail=f"Admin subaction={subaction}",
            trace_id=trace_id,
        )
