"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [chassis-bridge, handlers]
owner: engine-team
status: active
--- /L9_META ---

Action handlers for L9 chassis integration.
Handlers receive (tenant: str, payload: dict) and return dict.
"""

from __future__ import annotations

import functools
import logging
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

from neo4j.exceptions import ConstraintError

# T5-03: handlers.py is the sanctioned chassis bridge. Re-export chassis
# error types here so other engine modules can use them without importing
# chassis directly.
from chassis.errors import AuthorizationError, FeatureNotEnabled

# T5-03: handlers.py is the sanctioned chassis bridge. Re-export chassis
# error types here so other engine modules can use them without importing
# chassis directly.
from engine.auth.capabilities import (
    ACTION_PERMISSION_MAP,
    PERM_SCORE_READ,
    CapabilitySet,
    check_action_permission,
    get_capability_validator,
)
from engine.compliance.engine import ComplianceEngine
from engine.config.loader import DomainPackLoader
from engine.config.schema import DomainSpec, SyncEndpointSpec, SyncStrategy
from engine.gates.compiler import GateCompiler
from engine.gds.scheduler import GDSScheduler
from engine.graph.driver import ExpectedWriteOutcome, GraphDriver
from engine.scoring.assembler import ScoringAssembler
from engine.scoring.evidence_priority import (
    EvidenceBinding,
    EvidenceUnavailableError,
    ScoreRequestError,
    blocked_result,
    parse_score_request,
    score_cohort,
)
from engine.scoring.graph_evidence import (
    GraphEvidenceBinding,
    approved_relevance_from_spec,
    declared_evidence_types,
)
from engine.state import get_state
from engine.sync.generator import (
    RELATIONSHIP_EVIDENCE_SCHEMA,
    SyncGenerator,
    normalize_relationship_rows,
    observed_association_defects,
    ref_namespace_defects,
    relationship_merge_query,
    relationship_precheck_query,
    relationship_uniqueness_constraint_query,
    resolve_relationship_declaration,
    stored_evidence,
)
from engine.traversal.assembler import TraversalAssembler
from engine.traversal.resolver import ParameterResolutionError, ParameterResolver
from engine.utils.security import sanitize_label, tenant_id_defect

__all_chassis_reexports__ = ["FeatureNotEnabled"]

logger = logging.getLogger(__name__)

_CAPABILITY_AUTH_BYPASS_ENVS = frozenset({"dev", "local", "test"})


class EngineError(Exception):
    """Base engine error with structured context."""

    def __init__(self, message: str, *, action: str, tenant: str, detail: str | None = None) -> None:
        self.action = action
        self.tenant = tenant
        self.detail = detail
        super().__init__(message)


class ValidationError(EngineError):
    """Payload validation failure."""


class ObservedAssociationRejectedError(ValidationError):
    """D-9: an observed row claims a provider association CEG's own tenant spec does not declare.

    Its own class because a failure packet carries the error class name, not the
    message: the producer reads the name as "my spec mirror has drifted from CEG".
    """


class SyncRowRejectedError(ValidationError):
    """A node sync batch does not conform to the types the tenant spec declares.

    Raised before compliance logging or any Cypher, so nothing is written. The
    message lists every defect as ``row <i> <field>: expected <declared>, got
    <actual>``; ``defects`` carries the same list. Its own class so a producer
    can tell "my rows disagree with CEG's copy of the spec" from other
    validation failures by name.
    """

    def __init__(self, defects: list[str], *, action: str, tenant: str) -> None:
        self.defects = list(defects)
        message = f"Sync refused, nothing written: {len(self.defects)} defect(s): {'; '.join(self.defects)}"
        super().__init__(message, action=action, tenant=tenant)


class ExecutionError(EngineError):
    """Neo4j or runtime execution failure."""


class OutcomeVocabularyUndeclaredError(ValidationError):
    """An outcome write or read on a domain that declares no ``feedbackloop.outcome_node_label``.

    Its own class so a failure packet names the cause: the pack must declare its
    outcome node label (Stage 2 follow-up, ``outcome_vocabulary_required_enabled``).
    Not a ``ValueError``, so no outcome path swallows it as a malformed item.
    """


def _outcome_label(domain_spec: DomainSpec, tenant: str, action: str, operation: str) -> str:
    """The domain's sanitized outcome node label, or OutcomeVocabularyUndeclaredError."""
    from engine.config.outcome_vocabulary import OutcomeNodeLabelUndeclaredError, outcome_node_label

    try:
        return sanitize_label(outcome_node_label(domain_spec, operation))
    except OutcomeNodeLabelUndeclaredError as exc:
        raise OutcomeVocabularyUndeclaredError(str(exc), action=action, tenant=tenant) from exc


def init_dependencies(
    graph_driver: GraphDriver,
    domain_loader: DomainPackLoader,
    db_pool: Any | None = None,
) -> None:
    """Called by chassis/boot at startup to inject dependencies into EngineState.

    W4-01: Populates the EngineState singleton rather than module-level globals.
    W4-04: db_pool is an optional asyncpg pool used by ComplianceEngine for
    audit-flush persistence. None = compliance audit flush no-ops (soft dependency).
    """
    state = get_state()
    state._graph_driver = graph_driver
    state._domain_loader = domain_loader
    state._db_pool = db_pool
    import os

    allowlist_raw = os.getenv("TENANT_ALLOWLIST", "")
    if allowlist_raw.strip():
        state._tenant_allowlist = {t.strip() for t in allowlist_raw.split(",") if t.strip()}
        logger.info("Tenant allowlist configured: %s", state._tenant_allowlist)
    else:
        state._tenant_allowlist = None
        if _allowlist_fails_closed():
            # CEG-AUTH-01: in production an absent allowlist denies every tenant
            # (see _validate_tenant_access) rather than admitting every tenant.
            logger.error(
                "No TENANT_ALLOWLIST configured and L9_ENV=prod — every tenant request will be rejected "
                "until TENANT_ALLOWLIST is set"
            )
        else:
            logger.warning("No TENANT_ALLOWLIST configured — all tenants accessible")
    state._initialized = True


def _is_production() -> bool:
    """True when the engine runs with ``L9_ENV=prod``.

    Compares ``l9_env`` directly rather than reading ``settings.is_production``
    so a test that patches ``settings`` with a MagicMock does not read a truthy
    attribute as "production".
    """
    from engine.config.settings import settings as _env_settings

    return _env_settings.l9_env == "prod"


def _allowlist_fails_closed() -> bool:
    """True when an unset TENANT_ALLOWLIST must deny every tenant (CEG-AUTH-01).

    Production only, and only while ``tenant_allowlist_required_in_prod`` is on.
    The flag is compared with ``is False`` so a MagicMock-patched ``settings``
    keeps the fail-closed default.
    """
    from engine.config.settings import settings as _env_settings

    return _is_production() and _env_settings.tenant_allowlist_required_in_prod is not False


def _validate_tenant_access(tenant: str, action: str) -> None:
    """Reject requests for unauthorized tenants.

    The enforced control is ``TENANT_ALLOWLIST`` (resolved into EngineState at
    ``init_dependencies``). An unset allowlist admits every tenant outside
    production and denies every tenant in production (CEG-AUTH-01): an
    operator who forgot the allowlist on a prod deployment gets refusals and a
    log line naming the fix, not an open multi-tenant engine.

    CEG-AUTH-01 also removed a JWT ``allowed_tenants`` branch from here. The
    chassis auth middleware (``chassis/auth/auth.py``) decodes that claim onto
    ``request.state`` and defers the check "to the handler layer", but no
    caller ever forwarded the claim into the engine — the branch was
    unreachable, and keeping it documented an enforcement that did not exist.
    """
    state = get_state()
    allowlist = state.tenant_allowlist
    if allowlist is None:
        if _allowlist_fails_closed():
            raise ValidationError(
                f"Tenant '{tenant}' rejected: TENANT_ALLOWLIST is not configured and L9_ENV=prod "
                "fails closed (CEG-AUTH-01)",
                action=action,
                tenant=tenant,
            )
        return
    if tenant not in allowlist:
        raise ValidationError(
            f"Tenant '{tenant}' not in authorized allowlist",
            action=action,
            tenant=tenant,
        )


def _require_deps() -> tuple[GraphDriver, DomainPackLoader]:
    state = get_state()
    if not state.is_initialized:
        msg = "Dependencies not initialized. Call init_dependencies() first."
        raise RuntimeError(msg)
    return state.graph_driver, state.domain_loader


def _get_compliance_engine(domain_spec: DomainSpec) -> ComplianceEngine:
    """W4-04: Get-or-create ComplianceEngine from the singleton pool.

    Stores one ComplianceEngine per domain_id in EngineState.compliance_engines.
    Avoids per-request instantiation overhead.
    """
    state = get_state()
    domain_id = domain_spec.domain.id
    if domain_id not in state.compliance_engines:
        state.compliance_engines[domain_id] = ComplianceEngine(domain_spec, db_pool=state.db_pool)
    return state.compliance_engines[domain_id]


def _build_capability_set(domain_spec: DomainSpec) -> CapabilitySet | None:
    """Build a CapabilitySet from domain spec capabilities (W3-02).

    Returns None if no capabilities are defined in the spec.
    """
    if not domain_spec.capabilities:
        return None
    caps = [{"actions": c.actions, "allowed_subjects": c.allowed_subjects} for c in domain_spec.capabilities]
    return CapabilitySet(caps)


def _enforce_capability(tenant: str, action: str, domain_spec: DomainSpec) -> None:
    """W3-02/W3-03: Check capability-based access control.

    Production and other deployed environments require explicit grants for
    writes. Development, local, and test environments retain the established
    compatibility bypass; production and staging never fall back.
    """
    from engine.config.settings import settings as _cap_settings

    if not _cap_settings.capability_auth_enabled:
        if _cap_settings.l9_env not in _CAPABILITY_AUTH_BYPASS_ENVS:
            raise ValidationError(
                "CAPABILITY_AUTH_ENABLED=False is permitted only in dev, local, or test environments",
                action=action,
                tenant=tenant,
            )
        return

    if _cap_settings.l9_env in _CAPABILITY_AUTH_BYPASS_ENVS:
        return

    cap_set = _build_capability_set(domain_spec)
    required = ACTION_PERMISSION_MAP.get(action)
    if not check_action_permission(tenant, action, cap_set):
        required = required or action
        raise ValidationError(
            f"Tenant '{tenant}' lacks capability '{required}' for action '{action}'",
            action=action,
            tenant=tenant,
        )


def _require_key(payload: dict[str, Any], key: str, action: str, tenant: str) -> Any:
    """Extract required key with structured error."""
    if key not in payload:
        raise ValidationError(
            f"Missing required field '{key}' in {action} payload",
            action=action,
            tenant=tenant,
        )
    return payload[key]


_FEEDBACK_DISABLED_MESSAGE = (
    "Feedback loop is disabled. It runs only with FEEDBACK_ENABLED=True and "
    "feedbackloop.enabled: true in the domain pack."
)


def _feedback_effective(domain_spec: DomainSpec) -> bool:
    """FEEDBACK-01: the one feedback predicate.

    The outcome feedback loop runs only when the operator enables it globally
    (``FEEDBACK_ENABLED``) AND the domain pack opts in (``feedbackloop.enabled``).
    Every feedback site reads this predicate — learned-weight loading on match,
    the outcome sync type, the admin ``score_feedback`` / ``apply_weight_proposal``
    subactions and outcome propagation — so one policy has one gate. Before this
    the sites split between the two flags, and a domain with ``feedbackloop``
    off still propagated outcomes while a domain with it on could not sync them.
    """
    # Imported at call time: tests patch ``engine.config.settings.settings``.
    from engine.config.settings import settings as _feedback_settings

    return bool(_feedback_settings.feedback_enabled) and bool(domain_spec.feedbackloop.enabled)


# Allowed Cypher function patterns for enrich action (whitelist approach)
_SAFE_CYPHER_FUNCTIONS = frozenset(
    [
        "coalesce(",
        "toInteger(",
        "toFloat(",
        "toString(",
        "size(",
        "length(",
        "trim(",
        "toLower(",
        "toUpper(",
        "abs(",
        "round(",
        "ceil(",
        "floor(",
        "sqrt(",
        "log(",
        "exp(",
        "datetime(",
        "date(",
        "time(",
        "duration(",
        "point(",
        "distance(",
    ]
)

# Dangerous Cypher keywords that must be blocked
_DANGEROUS_PATTERNS = (
    "detach",  # must be before "delete" — DETACH DELETE reports detach
    "call",
    "create",
    "merge",
    "delete",
    "remove",
    "set",
    "match",
    "return",
    "with",
    "unwind",
    "foreach",
    "load",
    "using",
    "optional",
    "union",
    "apoc",
    "gds",
    "dbms",
    "db.",
    "//",
    "/*",
    "$$",
    "${",
)

# Valid property name pattern (alphanumeric + underscore, must start with letter/underscore)
_PROPERTY_NAME_RE = re.compile(r"^[a-zA-Z_]\w*$")

# Direct property access: n.<identifier> preceded by start-of-string or an operator/whitespace.
# Intentionally does NOT match n.<identifier> preceded by '(' to block substring(n.prop,...) bypass.
# Also matches Cypher parameter references ($param_name).
_DIRECT_PROPERTY_RE = re.compile(r"(?:^|[\s+\-*/])n\.[a-zA-Z_]\w*|\$[a-zA-Z_]\w*")


def _check_dangerous_patterns(expr_stripped: str, expr_lower: str) -> None:
    """Raise ValidationError if expression contains any dangerous pattern."""
    for pattern in _DANGEROUS_PATTERNS:
        if pattern in ("//", "/*", "$$", "${", "db."):
            if pattern in expr_lower:
                raise ValidationError(
                    f"Forbidden pattern '{pattern}' in expression",
                    action="enrich",
                    tenant="unknown",
                )
        else:
            keyword_re = re.compile(rf"\b{re.escape(pattern)}\b", re.IGNORECASE)
            if keyword_re.search(expr_stripped):
                raise ValidationError(
                    f"Forbidden keyword '{pattern}' in expression",
                    action="enrich",
                    tenant="unknown",
                )


def _try_safe_literal(expr_stripped: str, expr_lower: str) -> str | None:
    """
    Return expr_stripped if it is a safe literal (bool, null, number, quoted string).
    Return None if it is not a simple literal.
    Raise ValidationError if it looks like a quoted string but contains injections.
    """
    if expr_lower in ("true", "false", "null"):
        return expr_stripped
    try:
        float(expr_stripped)
    except ValueError:
        pass
    else:
        return expr_stripped
    # Single or double quoted string literal
    for quote in ("'", '"'):
        if expr_stripped.startswith(quote) and expr_stripped.endswith(quote) and expr_stripped.count(quote) == 2:
            inner = expr_stripped[1:-1]
            if "'" in inner or '"' in inner or "\\" in inner:
                raise ValidationError(
                    "String literals cannot contain quotes or escapes",
                    action="enrich",
                    tenant="unknown",
                )
            return expr_stripped
    return None


def _validate_property_refs_and_chars(expr_stripped: str) -> None:
    """Validate property names and allowed characters in the expression."""
    property_refs = re.findall(r"n\.([a-zA-Z_]\w*)", expr_stripped)
    for prop in property_refs:
        if not _PROPERTY_NAME_RE.match(prop):
            raise ValidationError(
                f"Invalid property name '{prop}' in expression",
                action="enrich",
                tenant="unknown",
            )
    allowed_operators = {"+", "-", "*", "/", "%", "(", ")", ",", ".", " ", "$"}
    allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    allowed_chars.update(allowed_operators)
    for char in expr_stripped:
        if char not in allowed_chars:
            raise ValidationError(
                f"Disallowed character '{char}' in expression",
                action="enrich",
                tenant="unknown",
            )


def _sanitize_expression(expr: str) -> str:
    """
    Sanitize Cypher expression for enrich action.
    Uses strict whitelist approach: only allow known-safe patterns.
    Raises ValidationError if expression contains potentially dangerous patterns.
    """
    expr_stripped = expr.strip()
    expr_lower = expr_stripped.lower()

    # Fast-path: safe literals bypass the dangerous-pattern check entirely.
    safe_literal = _try_safe_literal(expr_stripped, expr_lower)
    if safe_literal is not None:
        return safe_literal

    _check_dangerous_patterns(expr_stripped, expr_lower)

    has_safe_function = any(
        expr_lower.startswith(f.lower()) or f" {f.lower()}" in expr_lower or f"({f.lower()}" in expr_lower
        for f in _SAFE_CYPHER_FUNCTIONS
    )
    # Only allow direct property references (n.<identifier>), not function calls that wrap
    # property access (e.g. substring(n.name, 0, 3) is rejected here).
    has_property_access = bool(_DIRECT_PROPERTY_RE.search(expr_stripped))

    if not has_safe_function and not has_property_access:
        raise ValidationError(
            f"Expression '{expr_stripped}' does not match allowed patterns",
            action="enrich",
            tenant="unknown",
        )

    _validate_property_refs_and_chars(expr_stripped)
    return expr_stripped


_WEIGHT_FLOOR = 0.0
_WEIGHT_CEILING = 1.0
_WEIGHT_SUM_TOLERANCE = 1e-9


def _validate_match_weights(
    weights: dict[str, float],
    tenant: str,
    effective_weights: dict[str, Any] | None = None,
    known_weightkeys: set[str] | None = None,
) -> None:
    """W1-02: Validate user-supplied scoring weights.

    Each weight must be in [0, 1] and the total sum must be <= 1.0.

    RU-009 / CEG-COR-018: when ``effective_weights`` is given (the per-dimension
    weights the assembler will apply for the match direction: request override,
    else spec default) the ceiling is checked against their sum, not against the
    supplied keys alone; a key no active dimension declares
    (``known_weightkeys``) is rejected (R1a-DEC-06).
    """
    for key, val in weights.items():
        try:
            w = float(val)
        except (TypeError, ValueError) as exc:
            msg = f"Weight '{key}' is not a valid number"
            raise ValidationError(msg, action="match", tenant=tenant) from exc
        if w < _WEIGHT_FLOOR or w > _WEIGHT_CEILING:
            msg = f"Weight '{key}' = {w} is outside allowed range [{_WEIGHT_FLOOR}, {_WEIGHT_CEILING}]"
            raise ValidationError(msg, action="match", tenant=tenant)

    if known_weightkeys is not None:
        unknown = sorted(set(weights) - known_weightkeys)
        if unknown:
            msg = f"Unknown weight key(s) {unknown}: no active scoring dimension declares them"
            raise ValidationError(msg, action="match", tenant=tenant)

    source = weights if effective_weights is None else effective_weights
    weight_sum = 0.0
    for key, val in source.items():
        try:
            number = float(val)
        except (TypeError, ValueError) as exc:
            msg = f"Weight '{key}' is not a valid number"
            raise ValidationError(msg, action="match", tenant=tenant) from exc
        if not math.isfinite(number):
            msg = f"Weight '{key}' is not a finite number"
            raise ValidationError(msg, action="match", tenant=tenant)
        weight_sum += number
    if weight_sum > _WEIGHT_CEILING + _WEIGHT_SUM_TOLERANCE:
        msg = f"Weights sum to {weight_sum:.4f}, exceeding {_WEIGHT_CEILING}"
        raise ValidationError(msg, action="match", tenant=tenant)


_FALLBACK_CANDIDATE_ID_PROPERTY = "entity_id"


def _candidate_id_property(domain_spec: DomainSpec, label: str) -> str:
    """The property that identifies a candidate node, as the domain declares it.

    Stage 2 (P4): `handle_match` read a hardcoded `entity_id` off every
    candidate for Pareto ids and causal explanations — a PlasticOS convention
    no other pack is obliged to share. Resolution order: the sync endpoint's
    `idproperty` for this label (the key the engine itself MERGEs on), else the
    node's first `required` property, else `entity_id`.
    """
    from engine.config.settings import settings as _id_settings

    if not _id_settings.spec_declared_candidate_id:
        return _FALLBACK_CANDIDATE_ID_PROPERTY
    if domain_spec.sync:
        for endpoint in domain_spec.sync.endpoints:
            if endpoint.targetnode == label and endpoint.idproperty:
                return endpoint.idproperty
    for node in domain_spec.ontology.nodes:
        if node.label == label:
            for prop in node.properties:
                if prop.required:
                    return prop.name
    return _FALLBACK_CANDIDATE_ID_PROPERTY


def _namespaced_entity_ref(value: Any) -> str | None:
    """A stored ``namespace:id`` identity, or None.

    The pattern is the contract ``ENTITY_REF_PATTERN``. A bare id is not given
    a namespace, and a namespaced id is returned unchanged.
    """
    from engine.models.payloads import ENTITY_REF_PATTERN

    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(ENTITY_REF_PATTERN, text) is None:
        return None
    return text


_SCORE_IDENTITY_ABS_TOL = 1e-12


def _finite_match_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _same_score(left: float, right: float) -> bool:
    """True when two scores are identical at the normalization tolerance."""
    return math.isclose(left, right, rel_tol=0.0, abs_tol=_SCORE_IDENTITY_ABS_TOL)


def _match_score_scale(score: float | None, *, normalized: bool) -> str:
    if score is not None and (normalized or 0.0 <= score <= 1.0):
        return "0_to_1"
    return "unnormalized_declared"


def _label_declares(domain_spec: DomainSpec, label: str, prop: str) -> bool:
    for node in domain_spec.ontology.nodes:
        if node.label == label and any(item.name == prop for item in node.properties):
            return True
    return False


def _capacity_evidence(
    domain_spec: DomainSpec,
    label: str,
    node: dict[str, Any],
    query: dict[str, Any],
) -> dict[str, float | None] | None:
    """Actual capacity, requested volume, and the shortfall when both are known.

    Unknown capacity stays unknown. A shortfall is the positive gap; it is not
    rewritten into a claim that the capacity covers the request.
    """
    if not _label_declares(domain_spec, label, "capacity_tons_month"):
        return None
    if not any(field.name == "volume_tons" for field in domain_spec.queryschema.fields):
        return None
    capacity = _finite_match_score(node.get("capacity_tons_month"))
    volume = _finite_match_score(query.get("volume_tons"))
    shortfall: float | None = None
    if capacity is not None and volume is not None:
        shortfall = volume - capacity if volume > capacity else 0.0
    return {"capacity_tons_month": capacity, "volume_tons": volume, "shortfall_tons": shortfall}


def _attach_match_consumer_fields(
    candidates: list[Any],
    *,
    id_property: str,
    active_dimensions: list[str],
    normalized: bool,
    domain_spec: DomainSpec,
    query: dict[str, Any],
    candidate_label: str,
) -> None:
    """Lift the fields a match consumer reads onto each returned row.

    The candidate identity is a stored namespaced ref, or the domain id when
    that id is already namespaced. A bare id is omitted. A non-finite
    score is not eligible and receives no rank. A finite dimension projection is
    evidence only when its source was stored; a clamped zero from an absent
    source stays missing evidence. The draft MatchResponse model is not emitted.
    """
    next_rank = 1
    for row in candidates:
        if not isinstance(row, dict):
            continue
        node = row.get("candidate")
        node = node if isinstance(node, dict) else {}
        entity_ref = (
            _namespaced_entity_ref(row.get("entity_ref"))
            or _namespaced_entity_ref(node.get("entity_ref"))
            or _namespaced_entity_ref(node.get(id_property))
        )
        if entity_ref is not None:
            row["entity_ref"] = entity_ref
        elif "entity_ref" in row:
            row.pop("entity_ref")
        score = _finite_match_score(row.get("score"))
        # A null or non-finite score is not a zero and is not eligible. Writing
        # it back stops NaN from leaving the row as a number a consumer can rank.
        row["score"] = score
        eligible = score is not None
        row["eligible"] = eligible
        if eligible:
            row["rank"] = next_rank
            next_rank += 1
        else:
            row["rank"] = None
        row["score_scale"] = _match_score_scale(score, normalized=normalized)
        raw_failed = row.get("failed_gates")
        row["failed_gates"] = list(raw_failed) if isinstance(raw_failed, list) else []
        raw_dims = row.get("dimension_scores")
        dims = raw_dims if isinstance(raw_dims, dict) else {}
        raw_absent = row.get("absent_sources")
        absent = {str(name) for name in raw_absent} if isinstance(raw_absent, list) else set()
        contributions: list[dict[str, Any]] = []
        present: set[str] = set()
        for name, raw in dims.items():
            if name in {"candidate", "score"} or str(name) in absent:
                continue
            number = _finite_match_score(raw)
            if number is None:
                continue
            present.add(str(name))
            contributions.append({"feature_id": str(name), "contribution": number, "evidence_refs": []})
        row["feature_contributions"] = contributions
        row["missing_evidence"] = [name for name in active_dimensions if name not in present]
        capacity = _capacity_evidence(domain_spec, candidate_label, node, query)
        if capacity is not None:
            row["capacity_evidence"] = capacity


def _candidate_id(candidate: Any, id_property: str, fallback: Any) -> str:
    """Read the declared id off a returned candidate node, falling back to `entity_id`, then `fallback`."""
    if not isinstance(candidate, dict):
        return str(fallback)
    value = candidate.get(id_property)
    if value is None and id_property != _FALLBACK_CANDIDATE_ID_PROPERTY:
        value = candidate.get(_FALLBACK_CANDIDATE_ID_PROPERTY)
    return str(fallback if value is None else value)


def _declared_id_predicate(
    domain_spec: DomainSpec,
    variable: str,
    param: str,
    match_direction: str | None = None,
) -> str | None:
    """RU-002 / CEG-COR-002: a label-scoped candidate-id predicate, or None for legacy.

    Scoped to the candidate label(s) of ``match_direction`` when it names a
    declared direction, else to every declared candidate label, each matched on
    its own declared id property (``_candidate_id_property``):
    ``(c:L1 AND c.p1 = $param) OR (c:L2 AND c.p2 = $param)``. Labels and
    properties pass ``sanitize_label``; the value stays a parameter (C-9).
    Returns None with ``spec_declared_candidate_id`` off or no declared
    candidate label, so the caller keeps its ``{entity_id: $param}`` lookup.
    """
    from engine.config.settings import settings as _id_settings

    if not _id_settings.spec_declared_candidate_id:
        return None
    candidates = domain_spec.matchentities.candidate
    labels = [c.label for c in candidates if match_direction and c.matchdirection == match_direction]
    if not labels:
        labels = [c.label for c in candidates]
    var = sanitize_label(variable)
    key = sanitize_label(param)
    parts: list[str] = []
    for label in dict.fromkeys(labels):
        prop = sanitize_label(_candidate_id_property(domain_spec, label))
        parts.append(f"({var}:{sanitize_label(label)} AND {var}.{prop} = ${key})")
    return " OR ".join(parts) or None


def _annotate_weighted_confidence(
    checker: Any,
    candidates: list[Any],
    effective_weights: dict[str, float],
) -> None:
    """RU-032 / CEG-COR-167: run the W2-03 confidence checks with weighted monoculture.

    The monoculture check measures one dimension's share of the *score*, so it
    is fed ``weight * value`` per dimension (the coefficients the assembler
    emitted). Dimensional agreement compares the raw values and keeps reading
    them, and each row's ``dimension_scores`` stays the raw projection. The
    annotation shape matches ``ConfidenceChecker.annotate_candidates``.
    """
    contribution_views: list[dict[str, Any]] = []
    for cand in candidates:
        raw = cand.get("dimension_scores") if isinstance(cand, dict) else None
        contributions: dict[str, float] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if isinstance(value, (int, float)):
                    contributions[name] = value * effective_weights.get(name, 1.0)
        contribution_views.append({"dimension_scores": contributions})

    mono = {f["candidate_index"]: f for f in checker.check_monoculture(contribution_views)}
    agreement = {f["candidate_index"]: f for f in checker.check_dimensional_agreement(candidates)}

    for idx, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            continue
        flags: list[str] = []
        detail: dict[str, Any] = {}
        if idx in mono:
            flags.append(mono[idx]["flag"])
            detail["dominant_dimension"] = mono[idx]["dominant_dimension"]
            detail["contribution"] = mono[idx]["contribution"]
        if idx in agreement:
            flags.append(agreement[idx]["flag"])
            detail["dimensional_agreement"] = agreement[idx]["confidence"]
        if flags:
            cand["confidence_flag"] = flags[0] if len(flags) == 1 else flags
            cand["confidence_detail"] = detail


def _apply_pareto_precut(
    results: list[dict[str, Any]],
    domain_spec: DomainSpec,
    *,
    id_property: str,
    top_n: int,
    pool_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """SI-06: decision arbitration on the fetched pool, before the ``top_n`` cut.

    Hard / soft constraints apply to the whole pool first, exactly as the
    post-cut path applies them to the cut list; the survivors are then ordered
    by Pareto front over the declared objectives (``pareto_precut``) and cut.
    """
    from engine.scoring.pareto_integrator import apply_constraint_penalties, pareto_precut

    arbitration = domain_spec.decision_arbitration
    constraint_dicts = [
        {"dimension": c.dimension, "threshold": c.threshold, "hard": c.hard, "penalty": c.penalty}
        for c in arbitration.constraints
    ]
    if constraint_dicts:
        results = apply_constraint_penalties(results, constraint_dicts)
    objectives = arbitration.pareto_config.objectives
    return pareto_precut(
        results,
        [obj.dimension for obj in objectives],
        top_n=top_n,
        pool_limit=pool_limit,
        candidate_id_key=id_property,
        directions={obj.dimension: obj.direction for obj in objectives},
    )


def _match_request_as_legacy(tenant: str, payload: dict[str, Any], domain_loader: DomainPackLoader) -> dict[str, Any]:
    """RU-012 / CEG-COR-063 (R1a-DEC-04): admit a contract-versioned MatchRequest.

    A payload without ``contract_version`` is the legacy MatchPayload and is
    returned unchanged. One with it is validated as
    ``engine.models.payloads.MatchRequest`` against the tenant's
    ``DomainContract`` (domain, declared direction, extra=forbid, transport
    fields refused) and mapped onto the legacy keys handle_match reads:
    ``direction`` -> ``match_direction``, ``query.facts`` -> ``query``,
    ``top_n``. Gated by ``match_request_contract_admission``.
    """
    from engine.config.settings import settings as _contract_settings

    if "contract_version" not in payload or not _contract_settings.match_request_contract_admission:
        return payload
    from pydantic import ValidationError as _PydanticValidationError

    from engine.models.payloads import DomainContract, MatchRequest, admit_payload

    contract = DomainContract.from_spec(domain_loader.load_domain(tenant))
    try:
        request = admit_payload(MatchRequest, payload, contract)
    except _PydanticValidationError as exc:
        raise ValidationError(
            f"MatchRequest admission failed: {exc}",
            action="match",
            tenant=tenant,
            detail="RU-012: contract_version payloads are admitted as MatchRequest",
        ) from exc
    return {"query": dict(request.query.facts), "match_direction": request.direction, "top_n": request.top_n}


async def handle_match(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Gate-then-score graph traversal."""
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, "match")
    start_time = time.monotonic()

    payload = _match_request_as_legacy(tenant, payload, domain_loader)
    query = _require_key(payload, "query", "match", tenant)
    match_direction = _require_key(payload, "match_direction", "match", tenant)

    raw_top_n = payload.get("top_n", 10)
    try:
        top_n = max(1, min(int(raw_top_n), 1000))
    except (TypeError, ValueError):
        top_n = 10

    weights = payload.get("weights", {})

    from engine.config.settings import settings

    # W1-02: Validate user-supplied weights are in [0, 1] and sum to <= 1.0
    effective_weight_check = bool(settings.match_effective_weight_validation)
    if weights and settings.score_clamp_enabled and not effective_weight_check:
        _validate_match_weights(weights, tenant)

    domain_spec = domain_loader.load_domain(tenant)
    _enforce_capability(tenant, "match", domain_spec)

    # RU-009 / CEG-COR-018: validate the weights scoring will actually apply for
    # this direction (one merge, in the assembler), not only the supplied keys.
    if weights and settings.score_clamp_enabled and effective_weight_check:
        weight_merge = ScoringAssembler(domain_spec)
        _validate_match_weights(
            weights,
            tenant,
            effective_weights=weight_merge.merged_request_weights(match_direction, weights),
            known_weightkeys={
                d.weightkey
                for d in domain_spec.scoring.dimensions
                if not (d.matchdirections and match_direction not in d.matchdirections)
            },
        )

    compliance = _get_compliance_engine(domain_spec)
    if compliance.enabled:
        compliance.validate_gates(domain_spec.gates, tenant=tenant)
        compliance.validate_scoring(domain_spec.scoring.dimensions, tenant=tenant)
        query = compliance.check_match_request(
            tenant=tenant,
            query=query,
            match_direction=match_direction,
        )

    from engine.config.settings import settings as _param_settings

    # RU-016 / CEG-COR-171: declared queryschema defaults are part of the query
    # the derived expressions evaluate over; backfill them (caller values win)
    # on the post-compliance query before resolving. Compliance saw the
    # caller's query above.
    if _param_settings.query_defaults_before_derived:
        query = {
            **{f.name: f.default for f in domain_spec.queryschema.fields if f.name not in query},
            **query,
        }

    resolver = ParameterResolver(domain_spec)
    try:
        resolved_query = resolver.resolve_parameters(query)
    except ParameterResolutionError as exc:
        raise ValidationError(
            str(exc),
            action="match",
            tenant=tenant,
            detail="Derived parameter resolution failed (W1-05 strict mode)",
        ) from exc

    for field in domain_spec.queryschema.fields:
        if field.name not in resolved_query:
            resolved_query[field.name] = field.default

    # Scoring dimensions may reference query parameters (queryprop) that are
    # not declared in queryschema (e.g. a communitymatch dimension's queryprop).
    # Backfill them with null so the generated Cypher never fails with
    # Neo.ClientError.Statement.ParameterMissing — the scoring expressions
    # already define null-handling defaults.
    for dim in domain_spec.scoring.dimensions:
        if dim.queryprop and dim.queryprop not in resolved_query:
            resolved_query[dim.queryprop] = None

    gate_compiler = GateCompiler(domain_spec)
    # RU-010 / CEG-COR-023 (R1a-DEC-03, third alternative): W1-03 runs on the
    # final resolved parameters (defaults backfilled, derived resolved) for the
    # gates of this direction. strict_null_gates rejects a null undeclared or
    # required gate parameter; a null optional one follows the gate's
    # nullbehavior and is logged. Off: no pre-pass (compiled Cypher only).
    if settings.strict_null_gates:
        from engine.gates.compiler import UnboundGateParameterError

        try:
            gate_compiler.validate_gates(resolved_query, match_direction=match_direction, reject_unbound=True)
        except UnboundGateParameterError as exc:
            raise ValidationError(
                str(exc),
                action="match",
                tenant=tenant,
                detail="W1-03 strict_null_gates: undeclared or required gate parameter is null",
            ) from exc
    where_clause = gate_compiler.compile_all_gates(match_direction)

    traversal_assembler = TraversalAssembler(domain_spec)
    traversal_clauses = traversal_assembler.assemble_traversal(match_direction)

    scoring_assembler = ScoringAssembler(domain_spec, graph_driver=graph_driver)
    # Convergence loop: load learned weights from feedback loop (FEEDBACK-01 predicate)
    if _feedback_effective(domain_spec) and domain_spec.feedbackloop.signal_weights.enabled:
        await scoring_assembler.load_learned_weights()
    scoring_clause, pareto_metadata = scoring_assembler.assemble_scoring_clause(match_direction, weights)

    candidate_labels = [c.label for c in domain_spec.matchentities.candidate if c.matchdirection == match_direction]
    if not candidate_labels:
        raise ValidationError(
            f"No candidate entity for direction {match_direction!r}",
            action="match",
            tenant=tenant,
        )
    from engine.config.settings import settings as _match_settings

    # RU-001 / CEG-COR-001: project the active dimension scores as one map so
    # Pareto, arbitration and confidence read real values. Aliases are the
    # sanitized names the scoring WITH binds; zero active dimensions -> `{}`.
    project_dimensions = bool(_match_settings.match_dimension_projection)
    dim_projection = ""
    absent_expr = scoring_assembler.absent_sources_expr()
    if project_dimensions:
        dim_aliases = [sanitize_label(name) for name in scoring_assembler.last_active_dimension_names]
        dim_map = ", ".join(f"{alias}: {alias}" for alias in dim_aliases)
        dim_projection = f"{{{dim_map}}} AS dimension_scores"
    # RU-036 / CEG-COR-007: one row per candidate (its best-scoring path) before
    # ORDER BY / LIMIT, so top_n counts distinct candidates (R2-DEC-05 option A).
    if _match_settings.match_collapse_traversal_rows:
        if project_dimensions:
            row_projection = f"WITH candidate, score, {dim_projection}, {absent_expr} AS absent_sources\n"
            best_row = "{score: score, dimension_scores: dimension_scores, absent_sources: absent_sources}"
            return_line = (
                "RETURN candidate, best.score AS score, best.dimension_scores AS dimension_scores, "
                "best.absent_sources AS absent_sources\n"
            )
        else:
            row_projection = "WITH candidate, score\n"
            best_row = "{score: score}"
            return_line = "RETURN candidate, best.score AS score\n"
        return_line = (
            row_projection
            + "ORDER BY score DESC\n"
            + f"WITH candidate, head(collect({best_row})) AS best\n"
            + return_line
        )
    elif project_dimensions:
        return_line = f"RETURN candidate, score, {dim_projection}, {absent_expr} AS absent_sources\n"
    else:
        return_line = "RETURN candidate, score\n"
    # RU-011 / CEG-COR-033: a WHERE directly after OPTIONAL MATCH belongs to it
    # and never removes a row; `WITH *` makes the gates a row filter while every
    # traversal variable stays in scope.
    gate_row_boundary = "WITH *\n" if _match_settings.gate_where_row_scope else ""
    # SI-06: with pareto_precut_enabled and declared arbitration objectives the
    # same `LIMIT $top_n` binds a bounded pool larger than top_n; arbitration
    # orders that pool by Pareto front and the cut to top_n happens after it.
    arbitration_spec = domain_spec.decision_arbitration
    precut_active = bool(
        _match_settings.pareto_precut_enabled and arbitration_spec.enabled and arbitration_spec.pareto_config.objectives
    )
    fetch_limit = top_n
    if precut_active:
        from engine.scoring.pareto_integrator import precut_pool_limit

        fetch_limit = precut_pool_limit(top_n)
    cypher = (
        f"MATCH (candidate:{sanitize_label(candidate_labels[0])})\n"
        + "\n".join(traversal_clauses)
        + "\n"
        + gate_row_boundary
        + f"WHERE {where_clause}\n"
        + f"{scoring_clause}\n"
        + return_line
        + "ORDER BY score DESC\n"
        + "LIMIT $top_n"
    )

    try:
        # CEG-009b: gate literals (`queryparam: 85.0`, strictwhen constants) are
        # bound by the compiler under generated names; merge them like scoring's.
        parameters = {
            **resolved_query,
            "top_n": fetch_limit,
            **gate_compiler.last_query_params,
            **scoring_assembler.last_query_params,
        }
        results = await graph_driver.execute_query(
            cypher=cypher,
            parameters=parameters,
            database=domain_spec.domain.id,
        )
    except Exception as exc:
        logger.exception("Match query failed for tenant=%s", tenant)
        raise ExecutionError(
            "Match query execution failed",
            action="match",
            tenant=tenant,
            detail="Query execution error",
        ) from exc

    precut_meta: dict[str, Any] | None = None
    if precut_active and results:
        results, precut_meta = _apply_pareto_precut(
            list(results),
            domain_spec,
            id_property=_candidate_id_property(domain_spec, candidate_labels[0]),
            top_n=top_n,
            pool_limit=fetch_limit,
        )

    # Build Pareto candidates from results and compute front metadata
    pareto_candidates_input: list[Any] | None = None
    if results:
        from engine.config.settings import settings as _settings

        # One declared id for every consumer of candidate identity below: the
        # Pareto front, the arbitration filter and the causal explanations.
        id_property = _candidate_id_property(domain_spec, candidate_labels[0])

        if _settings.pareto_enabled:
            from engine.scoring.pareto import ParetoCandidate

            def _pareto_dims(row: dict[str, Any]) -> dict[str, float]:
                projected = row.get("dimension_scores") if project_dimensions else None
                source = projected if isinstance(projected, dict) else row
                # Only int | float values pass the isinstance filter: float() cannot raise.
                return {
                    k: float(v)
                    for k, v in source.items()
                    if k not in ("candidate", "score") and isinstance(v, (int, float))
                }

            pareto_candidates_input = [
                ParetoCandidate(
                    candidate_id=_candidate_id(r.get("candidate", {}), id_property, i),
                    dimension_scores=_pareto_dims(r),
                )
                for i, r in enumerate(results)
            ]
            # Re-run assembler with Pareto candidates to get front metadata
            _, pareto_metadata = scoring_assembler.assemble_scoring_clause(
                match_direction, weights, pareto_candidates=pareto_candidates_input
            )

        if precut_meta is not None:
            # SI-06: constraints and the front were applied to the pool above.
            if pareto_metadata is None:
                pareto_metadata = {}
            pareto_metadata["arbitration"] = precut_meta

        # --- Milestone 2.1: Spec-driven constraint enforcement ---
        elif domain_spec.decision_arbitration.enabled:
            from engine.scoring.pareto_integrator import (
                apply_constraint_penalties,
                apply_pareto_filter,
            )

            constraint_dicts = [
                {
                    "dimension": c.dimension,
                    "threshold": c.threshold,
                    "hard": c.hard,
                    "penalty": c.penalty,
                }
                for c in domain_spec.decision_arbitration.constraints
            ]

            if constraint_dicts:
                results = apply_constraint_penalties(list(results), constraint_dicts)

            # Spec-driven Pareto filter using declared objectives
            arb_config = domain_spec.decision_arbitration.pareto_config
            arb_dims = [obj.dimension for obj in arb_config.objectives]
            if arb_dims and len(results) > 1:
                # RU-019: forward declared directions and front_size_limit
                # (ignored by the filter while arbitration_objective_direction is off).
                arb_pareto = apply_pareto_filter(
                    results,
                    arb_dims,
                    candidate_id_key=id_property,
                    directions={obj.dimension: obj.direction for obj in arb_config.objectives},
                    front_size_limit=arb_config.front_size_limit,
                )
                if pareto_metadata is None:
                    pareto_metadata = {}
                pareto_metadata["arbitration"] = {
                    "frontsize": arb_pareto["frontsize"],
                    "pruned_pct": arb_pareto["pruned_pct"],
                    "nondominated_ids": arb_pareto["nondominated"],
                }
                if _settings.arbitration_objective_direction:
                    # RU-019: non-dominated ids dropped by front_size_limit are
                    # reported, not silently folded into pruned_pct.
                    pareto_metadata["arbitration"]["trimmed_ids"] = arb_pareto["trimmed"]

    execution_time_ms = (time.monotonic() - start_time) * 1000

    # --- W2-04: Score Normalization ---
    from engine.config.settings import settings as _w2_settings

    candidates_out = list(results)
    scoring_meta: dict[str, Any] = {
        "weights_used": weights,
        "normalization_applied": False,
    }

    if candidates_out:
        # Null and non-finite scores are not zeros. They stay None, stay out of
        # min/max, and are never the singleton that normalization promotes to 1.0.
        # A finite zero remains a real score.
        finite_scores: list[float] = []
        for cand in candidates_out:
            if not isinstance(cand, dict):
                continue
            finite = _finite_match_score(cand.get("score"))
            if finite is None:
                cand["score"] = None
                continue
            cand["score"] = finite
            finite_scores.append(finite)
        if finite_scores:
            scoring_meta["raw_max"] = max(finite_scores)
            scoring_meta["raw_min"] = min(finite_scores)
        else:
            scoring_meta["raw_max"] = 0.0
            scoring_meta["raw_min"] = 0.0

        if _w2_settings.score_normalize and finite_scores:
            raw_max = scoring_meta["raw_max"]
            raw_min = scoring_meta["raw_min"]
            score_range = raw_max - raw_min
            changed = False
            if score_range > 0:
                for cand in candidates_out:
                    if isinstance(cand, dict) and isinstance(cand.get("score"), (int, float)):
                        scaled = round((cand["score"] - raw_min) / score_range, 6)
                        changed = changed or not _same_score(float(scaled), float(cand["score"]))
                        cand["score"] = scaled
            elif len(finite_scores) == 1 and len(candidates_out) == 1 and isinstance(candidates_out[0], dict):
                if not _same_score(float(candidates_out[0]["score"]), 1.0):
                    candidates_out[0]["score"] = 1.0
                    changed = True
            # A tied range leaves every finite score unchanged. Do not label it normalized.
            scoring_meta["normalization_applied"] = changed
    else:
        scoring_meta["raw_max"] = 0.0
        scoring_meta["raw_min"] = 0.0

    # --- W2-03: Confidence Checking ---
    if _w2_settings.confidence_check_enabled:
        # Attach per-dimension scores to candidates and run monoculture check
        dim_names = scoring_assembler.last_active_dimension_names
        for c in candidates_out:
            if isinstance(c, dict) and not c.get("dimension_scores"):
                dim_scores: dict[str, float] = {}
                for dn in dim_names:
                    if dn in c:
                        dim_scores[dn] = c[dn]
                if dim_scores:
                    c["dimension_scores"] = dim_scores

        from engine.scoring.confidence import ConfidenceChecker

        checker = ConfidenceChecker(
            monoculture_threshold=_w2_settings.monoculture_threshold,
            ensemble_max_divergence=_w2_settings.ensemble_max_divergence,
        )
        if _w2_settings.monoculture_weighted_contribution:
            _annotate_weighted_confidence(checker, candidates_out, scoring_assembler.last_effective_weights)
        else:
            candidates_out = checker.annotate_candidates(candidates_out)

    # BFS causal explanations when causal subgraph is enabled
    if domain_spec.causal.enabled and candidates_out:
        from engine.causal.serializer import CausalSubgraphSerializer

        serializer = CausalSubgraphSerializer(graph_driver, domain_spec)
        candidate_label = candidate_labels[0]
        id_property = _candidate_id_property(domain_spec, candidate_label)
        for c in candidates_out:
            if isinstance(c, dict):
                cand = c.get("candidate", {})
                entity_id = _candidate_id(cand, id_property, "") if isinstance(cand, dict) else ""
                if entity_id:
                    explanation = await serializer.serialize_neighborhood(
                        node_id=entity_id,
                        node_label=candidate_label,
                        max_depth=min(domain_spec.causal.chain_depth_limit, 2),
                        id_property=id_property,
                    )
                    c["match_explanation"] = explanation

    if candidates_out:
        _attach_match_consumer_fields(
            candidates_out,
            id_property=_candidate_id_property(domain_spec, candidate_labels[0]),
            active_dimensions=list(scoring_assembler.last_active_dimension_names),
            normalized=bool(scoring_meta["normalization_applied"]),
            domain_spec=domain_spec,
            query=resolved_query,
            candidate_label=candidate_labels[0],
        )

    response = {
        "status": "success",  # RU-014 / CEG-COR-075 (RETURN_VALUES.md)
        "candidates": candidates_out,
        "query_id": f"q_{uuid.uuid4().hex[:12]}",
        "match_direction": match_direction,
        # Consumers read `direction`. The runtime contract keeps
        # `match_direction`. This is not the unpublished MatchResponse envelope.
        "direction": match_direction,
        "total_candidates": len(candidates_out),
        "execution_time_ms": round(execution_time_ms, 2),
        "scoring_meta": scoring_meta,
    }
    if pareto_metadata:
        response["pareto"] = pareto_metadata

    # Flush audit entries (currently logs warning since db_pool=None;
    # will persist to PostgreSQL once provisioned)
    await compliance.flush_audit_for_request()

    if compliance.enabled:
        response = compliance.redact_response(response, tenant)
    return response


async def _sync_outcomes_unified(
    graph_driver: GraphDriver,
    domain_spec: DomainSpec,
    tenant: str,
    batch: list[Any],
) -> dict[str, Any]:
    """RU-006 / CEG-COR-015: sync ``outcome`` items through the handle_outcomes writer.

    Each item's ``outcome`` maps onto success|failure|partial
    (_SYNC_OUTCOME_VOCABULARY); the node is the domain's outcome label with the
    same properties, RESULTED_IN edge and causal check as handle_outcomes. Items
    that are not maps, carry an unknown outcome or a malformed ``fingerprint``
    are skipped, as unknown outcomes always were.
    """
    synced = 0
    rejected_count = 0
    for item in batch:
        if not isinstance(item, dict):
            continue
        outcome_val = _SYNC_OUTCOME_VOCABULARY.get(item.get("outcome"))  # type: ignore[arg-type]
        if outcome_val is None:
            continue
        match_id = item.get("match_id")
        create_resulted_in = _resulted_in_permitted(domain_spec, item)
        try:
            cypher, parameters = _build_outcome_write(
                domain_spec,
                tenant,
                outcome_id=f"out_{uuid.uuid4().hex[:12]}",
                match_id=match_id,
                candidate_id=item.get("chosen_candidate_id"),
                outcome=outcome_val,
                value=item.get("value"),
                fingerprint=item.get("fingerprint", {}),
                create_resulted_in=create_resulted_in,
            )
        except ValueError as exc:
            logger.warning("Skipping outcome sync item for match_id=%s: %s", match_id, exc)
            continue
        try:
            await graph_driver.execute_query(cypher=cypher, parameters=parameters, database=domain_spec.domain.id)
        except Exception:
            logger.warning("Failed to sync outcome for match_id=%s", match_id, exc_info=True)
            continue
        synced += 1
        if not create_resulted_in:
            rejected_count += 1
    response: dict[str, Any] = {"status": "success", "entity_type": "outcome", "synced_count": synced}
    if rejected_count:
        response["rejected_count"] = rejected_count
    return response


def _check_sync_rows(domain_spec: DomainSpec, endpoint_spec: Any, batch: list[Any], *, tenant: str) -> None:
    """Refuse a node sync batch whose rows disagree with the declared types (``SyncRowContract``).

    Relationship endpoints have their own row contract (``normalize_relationship_rows``).
    With ``sync_row_type_validation_enabled`` off nothing is checked here.
    """
    from engine.config.settings import settings as _row_settings
    from engine.sync.row_contract import SyncRowContract

    if endpoint_spec.targetedge or not _row_settings.sync_row_type_validation_enabled:
        return
    try:
        contract = SyncRowContract.from_spec(domain_spec, endpoint_spec)
    except ValueError as exc:
        raise ValidationError(
            f"Sync refused, nothing written: the endpoint's fields are not declared by the spec: {exc}",
            action="sync",
            tenant=tenant,
        ) from exc
    defects = contract.defects(batch, refuse_undeclared=_row_settings.sync_undeclared_property_refusal_enabled)
    if defects:
        raise SyncRowRejectedError(defects, action="sync", tenant=tenant)


async def handle_sync(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Batch UNWIND MERGE/MATCH SET for graph entities."""
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, "sync")

    entity_type = _require_key(payload, "entity_type", "sync", tenant)
    batch = _require_key(payload, "batch", "sync", tenant)

    if not isinstance(batch, list) or len(batch) == 0:
        raise ValidationError(
            "Sync batch must be a non-empty list",
            action="sync",
            tenant=tenant,
        )

    # --- W2-02: Outcome record type via sync ---
    # FEEDBACK-01: global flag AND domain opt-in; otherwise "outcome" is an
    # ordinary entity type and falls through to the configured sync path.
    if entity_type == "outcome" and _feedback_effective(domain_loader.load_domain(tenant)):
        domain_spec = domain_loader.load_domain(tenant)
        _enforce_capability(tenant, "sync", domain_spec)
        from engine.config.settings import settings as _sync_outcome_settings

        if _sync_outcome_settings.outcome_schema_unified:
            return await _sync_outcomes_unified(graph_driver, domain_spec, tenant, batch)
        # RU-002: the chosen candidate is found by its declared id property.
        outcome_candidate_predicate = _declared_id_predicate(domain_spec, "c", "candidate_id")
        synced = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            match_id = item.get("match_id")
            candidate_id = item.get("chosen_candidate_id")
            outcome_val = item.get("outcome")
            if outcome_val not in ("positive", "negative", "neutral"):
                continue
            outcome_id = f"out_{uuid.uuid4().hex[:12]}"
            cypher = """
            CREATE (o:Outcome {
                outcome_id: $outcome_id, match_id: $match_id,
                candidate_id: $candidate_id, outcome: $outcome,
                created_at: datetime(), tenant: $tenant
            })
            WITH o
            OPTIONAL MATCH (c {entity_id: $candidate_id})
            FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END |
                CREATE (c)-[:HAS_OUTCOME]->(o)
            )
            """
            if outcome_candidate_predicate is not None:
                cypher = cypher.replace(
                    "OPTIONAL MATCH (c {entity_id: $candidate_id})",
                    f"OPTIONAL MATCH (c) WHERE {outcome_candidate_predicate}",
                )
            try:
                await graph_driver.execute_query(
                    cypher=cypher,
                    parameters={
                        "outcome_id": outcome_id,
                        "match_id": match_id,
                        "candidate_id": candidate_id,
                        "outcome": outcome_val,
                        "tenant": tenant,
                    },
                    database=domain_spec.domain.id,
                )
                synced += 1
            except Exception:
                logger.warning("Failed to sync outcome for match_id=%s", match_id, exc_info=True)
        return {"status": "success", "entity_type": "outcome", "synced_count": synced}

    domain_spec = domain_loader.load_domain(tenant)
    _enforce_capability(tenant, "sync", domain_spec)
    if not domain_spec.sync:
        raise ValidationError("No sync endpoints configured", action="sync", tenant=tenant)

    endpoint_spec = next(
        (e for e in domain_spec.sync.endpoints if e.path.rstrip("/").rsplit("/", 1)[-1] == entity_type),
        None,
    )
    if not endpoint_spec:
        raise ValidationError(
            f"No sync endpoint for entity type {entity_type!r}",
            action="sync",
            tenant=tenant,
        )

    if endpoint_spec.targetedge:
        from chassis.errors import FeatureNotEnabled

        # Imported at call time: tests patch ``engine.config.settings.settings``.
        from engine.config.settings import settings as _relationship_settings

        # Refused before compliance logs the request: the audit trail must not
        # record a mutation the gate never let happen.
        if not _relationship_settings.relationship_sync_enabled:
            raise FeatureNotEnabled(
                "Relationship evidence sync",
                flag="RELATIONSHIP_SYNC_ENABLED",
                action="sync",
                tenant=tenant,
            )

    # D-13: node ids outside the tenant's declared ref namespaces are refused
    # before compliance records the request or anything touches the graph.
    identity_defects = ref_namespace_defects(domain_spec, endpoint_spec, batch)
    if identity_defects:
        raise ValidationError(
            f"Sync refused, nothing written: {'; '.join(identity_defects[:10])}",
            action="sync",
            tenant=tenant,
        )

    # D-9: an observed row's provider association must be declared in CEG's own
    # copy of the tenant spec, never only in the producer's mirror of it.
    from engine.config.settings import settings as _provenance_settings

    provenance_defects = observed_association_defects(
        endpoint_spec, batch, require=_provenance_settings.observed_association_required
    )
    if provenance_defects:
        raise ObservedAssociationRejectedError(
            f"Sync refused, nothing written: {'; '.join(provenance_defects[:10])}",
            action="sync",
            tenant=tenant,
        )

    # Typed sync contract: a node row is checked against the types CEG's own
    # copy of the tenant spec declares before compliance records the request
    # or anything touches the graph. One defect refuses the whole batch.
    _check_sync_rows(domain_spec, endpoint_spec, batch, tenant=tenant)

    # Run compliance checks on sync request
    compliance = _get_compliance_engine(domain_spec)
    from engine.config.settings import settings as _sync_settings

    # T05: a relationship batch can still be refused or rolled back after the
    # checks, so its mutation record is buffered only once the write committed.
    audit_on_commit = bool(endpoint_spec.targetedge) and _sync_settings.relationship_sync_audit_on_commit_enabled

    def _check_sync(engine: ComplianceEngine) -> None:
        engine.check_sync_request(tenant=tenant, entity_type=entity_type, batch=batch, endpoint_spec=endpoint_spec)

    if compliance.enabled:
        # Audit on commit: the checks run now on a pool-less engine that is never flushed.
        _check_sync(ComplianceEngine(domain_spec) if audit_on_commit else compliance)

    if endpoint_spec.targetedge:
        relationship_result = await _sync_relationships(
            graph_driver, domain_spec, endpoint_spec, entity_type=entity_type, batch=batch, tenant=tenant
        )
        if audit_on_commit and compliance.enabled:
            _check_sync(compliance)
        await compliance.flush_audit()
        return relationship_result

    # R3: Causal edge validation when domain has causal edges enabled
    rejected_count = 0
    if domain_spec.causal.enabled and payload.get("edge_type"):
        from engine.causal.causal_validator import CausalEdgeRuntimeValidator

        validator = CausalEdgeRuntimeValidator(domain_spec.causal)
        batch, rejected = validator.validate_batch(batch, payload["edge_type"])
        rejected_count = len(rejected)
        if not batch:
            return {
                "status": "all_rejected",
                "entity_type": entity_type,
                "synced_count": 0,
                "rejected_count": rejected_count,
            }

    generator = SyncGenerator(domain_spec)
    cypher = generator.generate_sync_query(endpoint_spec, batch)
    from engine.config.settings import settings as _projection_settings

    write_batch: list[Any] = batch
    undeclared: dict[str, int] = {}
    if _projection_settings.sync_declared_projection_enabled:
        # The generator's Cypher reads `row.node` / `row.children`: only what
        # the spec declares writable reaches SET; undeclared keys are dropped.
        write_batch, undeclared = generator.project_batch(endpoint_spec, batch)
        if undeclared:
            logger.warning(
                "sync %s: dropped undeclared properties (not written) %s",
                entity_type,
                ", ".join(f"{name}={count}" for name, count in undeclared.items()),
            )

    try:
        write_rows = await graph_driver.execute_query(
            cypher=cypher,
            parameters={"batch": write_batch, "tenant": tenant},
            database=domain_spec.domain.id,
        )
    except Exception as exc:
        raise ExecutionError(
            "Sync query execution failed",
            action="sync",
            tenant=tenant,
            detail=str(exc),
        ) from exc

    await compliance.flush_audit_for_request()
    result: dict[str, Any] = {
        "status": "success",
        "entity_type": entity_type,
        "synced_count": len(batch),
    }
    if undeclared:
        result["undeclared_properties"] = undeclared
    from engine.config.schema import SyncStrategy
    from engine.config.settings import settings as _sync_settings

    # RU-060 / CEG-COR-169: UNWINDMATCHSET updates only rows that matched an
    # existing node; the statement RETURNs that count. UNWINDMERGE unchanged.
    if _sync_settings.sync_count_from_write_result and endpoint_spec.batchstrategy == SyncStrategy.UNWINDMATCHSET:
        matched = 0
        if write_rows and isinstance(write_rows[0], dict):
            matched = int(write_rows[0].get("synced_count") or 0)
        result["synced_count"] = matched
        result["unmatched_count"] = len(batch) - matched
    if rejected_count > 0:
        result["rejected_count"] = rejected_count
    return result


def _bind_admin_domain(
    tenant: str,
    payload: dict[str, Any],
    *,
    required: bool,
    default: str | None = None,
) -> Any:
    """RU-003 / CEG-COR-003: the admin subaction's domain is the packet tenant's.

    Every domain-bearing admin subaction reads ``domain_id`` here. A required
    key still goes through ``_require_key`` (C-23); an optional one falls back
    to ``default``. With ``admin_tenant_bound_domain`` on, a value that differs
    from the packet tenant is rejected before any load_domain or driver call —
    the identity contract binds tenant and domain id, mirroring HEALTH-05.
    Off: the caller-chosen domain_id, exactly as before.
    """
    from engine.config.settings import settings as _admin_settings

    domain_id = _require_key(payload, "domain_id", "admin", tenant) if required else payload.get("domain_id", default)
    if _admin_settings.admin_tenant_bound_domain and domain_id is not None and domain_id != tenant:
        raise ValidationError(
            f"admin domain_id {domain_id!r} does not match the packet tenant {tenant!r}; "
            "admin subactions operate on the caller's own domain only",
            action="admin",
            tenant=tenant,
        )
    return domain_id


def _admin_response_contract(
    handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    """RU-014 / CEG-COR-075: stamp the C-23 response envelope on every admin success.

    Every return carries ``status`` (``success`` where the subaction set none;
    existing values are kept) and ``subaction`` (the request's). One wrapper at
    dispatch exit instead of edits at each of the subaction return sites.
    """

    @functools.wraps(handler)
    async def _stamped(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await handler(tenant, payload)
        result.setdefault("status", "success")
        result["subaction"] = payload["subaction"]
        return result

    return _stamped


@_admin_response_contract
async def handle_admin(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Admin: introspection, schema init, GDS trigger, capability delegation."""
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, "admin")
    subaction = _require_key(payload, "subaction", "admin", tenant)

    # RU-008 / CEG-COR-017: admin honours the tenant's spec capabilities like
    # sync/outcomes/enrich do (every admin permission maps to admin:write).
    # Call unconditionally so a production/staging deploy cannot skip the
    # CAPABILITY_AUTH_ENABLED=False fail-closed branch inside _enforce_capability.
    _enforce_capability(tenant, "admin", domain_loader.load_domain(tenant))

    if subaction == "list_domains":
        # Issue #400 F4: one admission pass yields the names and, per tenant,
        # the digest/version/stage the loader admitted (no path, no content).
        revisions = domain_loader.list_domain_revisions()
        return {
            "domains": [revision["tenant_id"] for revision in revisions],
            "served_revisions": {revision["tenant_id"]: dict(revision) for revision in revisions},
        }

    if subaction == "get_domain":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec, revision = domain_loader.load_domain_revision(domain_id)
        return {"domain": spec.model_dump(mode="json"), "served_revision": dict(revision)}

    if subaction == "init_schema":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        count = await _init_schema(graph_driver, spec)
        return {"status": "schema_initialized", "constraints_created": count}

    if subaction == "trigger_gds":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        job_name = _require_key(payload, "job_name", "admin", tenant)
        spec = domain_loader.load_domain(domain_id)
        scheduler = _get_or_create_scheduler(spec, graph_driver)
        result = await scheduler.trigger_job(job_name)
        return {"status": "triggered", "job": job_name, "result": result}

    # --- W2-01: Score Calibration ---
    if subaction == "calibration_run":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        from engine.scoring.calibration import ScoreCalibrator

        calibrator = ScoreCalibrator(spec)
        report = calibrator.generate_calibration_report(domain_id)
        return {"status": "calibration_report", **report}

    # --- W2-02: Score Feedback ---
    if subaction == "score_feedback":
        # The tenant id is the spec id (identity contract 2026-10-22), so the
        # caller's own pack decides the domain half of the predicate.
        if not _feedback_effective(domain_loader.load_domain(tenant)):
            return {"status": "disabled", "message": _FEEDBACK_DISABLED_MESSAGE}
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        # Read recent outcomes from Neo4j. The outcome node label is the domain's
        # (feedbackloop.outcome_node_label), not a fixed engine vocabulary.
        outcome_label = _outcome_label(spec, tenant, "admin", "admin score_feedback")
        from engine.config.settings import settings as _fb_schema_settings

        # RU-006: project the per-dimension scores the writers persist.
        unified_schema = bool(_fb_schema_settings.outcome_schema_unified)
        dimension_projection = ", o.dimension_scores AS dimension_scores" if unified_schema else ""
        outcome_cypher = f"""
        MATCH (o:{outcome_label})
        WHERE o.tenant = $tenant
        RETURN o.outcome AS outcome, o.candidate_id AS candidate_id,
               o.match_id AS match_id, o.value AS value{dimension_projection}
        ORDER BY o.created_at DESC
        LIMIT 500
        """
        try:
            outcome_records = await graph_driver.execute_query(
                cypher=outcome_cypher,
                parameters={"tenant": tenant},
                database=spec.domain.id,
            )
        except Exception as exc:
            raise ExecutionError(
                "Failed to read outcomes for feedback",
                action="admin",
                tenant=tenant,
                detail=str(exc),
            ) from exc

        from engine.scoring.feedback import OutcomeFeedback

        if unified_schema:
            from engine.models.outcomes import decode_outcome_map

            outcome_records = [
                {**rec, "dimension_scores": decode_outcome_map(rec.get("dimension_scores"))} for rec in outcome_records
            ]
        feedback = OutcomeFeedback(outcome_records)
        result = feedback.compute_feedback()
        return {"status": "feedback_computed", **result}

    if subaction == "apply_weight_proposal":
        # The tenant id is the spec id (identity contract 2026-10-22), so the
        # domain whose weights are being applied is the caller's own.
        if not _feedback_effective(domain_loader.load_domain(tenant)):
            return {"status": "disabled", "message": _FEEDBACK_DISABLED_MESSAGE}
        proposed = _require_key(payload, "proposed_weights", "admin", tenant)
        current = _require_key(payload, "current_weights", "admin", tenant)
        from engine.scoring.feedback import OutcomeFeedback

        new_weights = OutcomeFeedback.apply_weights(current, proposed)
        return {"status": "weights_applied", "new_weights": new_weights}

    # ── W3-04: Capability Delegation ────────────────────────────
    if subaction == "delegate_capability":
        source_tenant = _require_key(payload, "source_tenant", "admin", tenant)
        target_tenant = _require_key(payload, "target_tenant", "admin", tenant)
        domain_id = _require_key(payload, "domain_id", "admin", tenant)
        actions = _require_key(payload, "actions", "admin", tenant)
        expires_in = payload.get("expires_in_seconds", 0.0)

        if not all(isinstance(value, str) and value for value in (source_tenant, target_tenant, domain_id)):
            raise ValidationError(
                "Capability delegation tenant and domain identifiers must be non-empty strings",
                action="admin",
                tenant=tenant,
            )
        if source_tenant != tenant:
            raise ValidationError(
                "Capability delegation source_tenant must equal the packet tenant",
                action="admin",
                tenant=tenant,
            )
        if (
            not isinstance(actions, list)
            or not actions
            or not all(isinstance(action, str) and action for action in actions)
        ):
            raise ValidationError(
                "Capability delegation actions must be a non-empty list of capability strings",
                action="admin",
                tenant=tenant,
            )
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in < 0:
            raise ValidationError(
                "Capability delegation expires_in_seconds must be a non-negative number",
                action="admin",
                tenant=tenant,
            )

        validator = get_capability_validator()

        # Only a previously registered, active capability owned by the packet
        # tenant may delegate. Payload values must never synthesize a root.
        parent_cap = None
        for cap in validator._registry.values():
            if cap.tenant_id == source_tenant and cap.is_active():
                if all(a in cap.allowed_actions for a in actions):
                    parent_cap = cap
                    break

        if parent_cap is None:
            raise ValidationError(
                "Capability delegation requires an existing active parent grant owned by the packet tenant",
                action="admin",
                tenant=tenant,
            )

        try:
            child_cap = validator.derive_capability(
                parent_cap,
                scope_restriction={
                    "domain_id": domain_id,
                    "allowed_actions": actions,
                },
                expires_in_seconds=expires_in,
                target_tenant=target_tenant,
            )
        except PermissionError as exc:
            raise ValidationError(
                f"Capability delegation exceeds parent authority: {exc}",
                action="admin",
                tenant=tenant,
            ) from exc

        # Audit the delegation
        domain_spec_for_audit = domain_loader.load_domain(domain_id) if domain_id != "*" else None
        compliance_engine = ComplianceEngine(domain_spec_for_audit) if domain_spec_for_audit else None
        if compliance_engine:
            compliance_engine.log_admin(tenant=tenant, subaction="delegate_capability")
        logger.info(
            "Capability delegated: %s → %s (domain=%s, actions=%s, cap_id=%s)",
            source_tenant,
            target_tenant,
            domain_id,
            actions,
            child_cap.capability_id,
        )
        return {
            "status": "delegated",
            "capability": child_cap.to_dict(),
            "delegation": {
                "source_tenant": source_tenant,
                "target_tenant": target_tenant,
                "domain_id": domain_id,
                "actions": actions,
            },
        }

    if subaction == "revoke_capability":
        capability_id = _require_key(payload, "capability_id", "admin", tenant)
        validator = get_capability_validator()
        from engine.config.settings import settings as _cap_settings

        # CAP-TENANT-01: a tenant revokes only what it owns or delegated. An
        # unowned id is refused exactly like an unknown one, before any
        # mutation, so revoke is not an oracle for other tenants' ids.
        tenant_bound = bool(_cap_settings.capability_tenant_binding_enabled)
        owned = not tenant_bound or validator.is_owned_by(capability_id, tenant)
        revoked = owned and validator.revoke_capability(capability_id)
        if not revoked:
            raise ValidationError(
                f"Capability '{capability_id}' not found in registry",
                action="admin",
                tenant=tenant,
            )

        # Audit the revocation
        logger.info("Capability revoked: %s (by tenant=%s)", capability_id, tenant)
        return {
            "status": "revoked",
            "capability_id": capability_id,
            "audit": validator.audit_summary(tenant if tenant_bound else None),
        }

    if subaction == "capability_audit":
        validator = get_capability_validator()
        from engine.config.settings import settings as _cap_settings

        scope = tenant if _cap_settings.capability_tenant_binding_enabled else None
        return {
            "status": "audit",
            "summary": validator.audit_summary(scope),
        }

    # --- W6-01: KGE Activation Pathway ---
    if subaction == "trigger_kge":
        from chassis.errors import FeatureNotEnabled
        from engine.config.settings import settings as _kge_s

        if not _kge_s.kge_enabled:
            raise FeatureNotEnabled("KGE", flag="KGE_ENABLED", action="admin", tenant=tenant)
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        if spec.kge is None:
            raise ValidationError(
                "Domain spec has no 'kge' section — cannot activate KGE",
                action="admin",
                tenant=tenant,
            )
        # Validate embedding dimension consistency (W1-01 cross-ref)
        if spec.kge.embeddingdim != _kge_s.kge_embedding_dim:
            raise ValidationError(
                f"Domain spec kge.embeddingdim={spec.kge.embeddingdim} differs from "
                f"settings.kge_embedding_dim={_kge_s.kge_embedding_dim}",
                action="admin",
                tenant=tenant,
            )
        # CEG-KGE-PARK: the only implemented model, CompoundE3D, is parked.
        # Refuse explicitly while parked; never fall back to another model.
        from engine.kge.parked import (
            COMPOUND_E3D,
            PARKED_FLAG,
            ParkedSubsystemError,
            construct_compound_e3d,
        )

        if spec.kge.model != COMPOUND_E3D:
            raise ValidationError(
                f"KGE model '{spec.kge.model}' has no implementation in this engine",
                action="admin",
                tenant=tenant,
            )
        try:
            model = construct_compound_e3d(spec.kge)  # instantiation validates config
        except ParkedSubsystemError as exc:
            raise FeatureNotEnabled(
                COMPOUND_E3D,
                flag=PARKED_FLAG,
                message=str(exc),
                action="admin",
                tenant=tenant,
                detail="parked",
                context=exc.to_context(),
            ) from exc
        config = model.config
        # SI-04: activation is reported only once the vector index is verified
        # to exist. A failed check or an empty SHOW INDEXES result refuses.
        index_name = spec.kge.vectorindex.name if spec.kge.vectorindex else f"{domain_id}_kge_index"
        try:
            rows = await graph_driver.execute_query(
                "SHOW INDEXES YIELD name WHERE name = $idx RETURN name",
                parameters={"idx": index_name},
                database=domain_id,
            )
        except Exception as exc:
            msg = f"KGE not activated: vector index check for '{index_name}' failed: {exc}"
            raise ExecutionError(msg, action="admin", tenant=tenant) from exc
        if not any(row.get("name") == index_name for row in rows or []):
            msg = f"KGE not activated: vector index '{index_name}' does not exist in database '{domain_id}'"
            raise ExecutionError(msg, action="admin", tenant=tenant)
        return {
            "status": "kge_activated",
            "domain_id": domain_id,
            "embedding_dim": config.embedding_dim,
            "training_relations": config.training_relations,
            "model": spec.kge.model,
            "smoke_test": {"ok": True, "detail": f"vector index '{index_name}' exists"},
        }

    if subaction == "kge_status":
        from engine.config.settings import settings as _kge_s
        from engine.kge.parked import PARKED_DECISION, PARKED_KGE_MODELS, is_parked_model, parked_subsystems_enabled

        domain_id = _bind_admin_domain(tenant, payload, required=False)
        kge_config_info: dict[str, Any] = {}
        if domain_id:
            try:
                spec = domain_loader.load_domain(domain_id)
                if spec.kge:
                    kge_config_info = {
                        "model": spec.kge.model,
                        "model_parked": is_parked_model(spec.kge.model),
                        "embeddingdim": spec.kge.embeddingdim,
                        "training_relations": list(spec.kge.trainingrelations),
                    }
            except Exception:
                kge_config_info = {"error": f"Could not load domain '{domain_id}'"}
        return {
            "status": "ok",
            "kge_enabled": _kge_s.kge_enabled,
            "kge_embedding_dim": _kge_s.kge_embedding_dim,
            "kge_confidence_threshold": _kge_s.kge_confidence_threshold,
            "parked": {
                "decision": PARKED_DECISION,
                "models": sorted(PARKED_KGE_MODELS),
                "active": not parked_subsystems_enabled(),
            },
            "domain_config": kge_config_info,
        }

    # --- W6-02: GDPR Erasure Endpoint ---
    if subaction == "erase_subject":
        from chassis.errors import FeatureNotEnabled
        from engine.compliance.pii import PIIHandler
        from engine.config.settings import settings as _gdpr_s

        if not _gdpr_s.gdpr_erasure_enabled:
            raise FeatureNotEnabled("GDPR Erasure", flag="GDPR_ERASURE_ENABLED", action="admin", tenant=tenant)
        data_subject_id = _require_key(payload, "data_subject_id", "admin", tenant)
        domain_id = _bind_admin_domain(tenant, payload, required=False, default=tenant)
        spec = domain_loader.load_domain(domain_id)
        # RU-066: the subject property is the domain's declared field, sanitized
        # before any query (an invalid name raises ValueError here).
        from engine.utils.security import sanitize_label as _gdpr_label

        subject_property = _gdpr_label(spec.compliance.gdpr_subject_id_field if spec.compliance else "data_subject_id")

        pii_handler = PIIHandler()

        if _gdpr_s.gdpr_dry_run:
            # Dry-run: compute what WOULD be erased without executing
            try:
                count_result = await graph_driver.execute_query(
                    f"MATCH (n {{{subject_property}: $dsid}}) RETURN count(n) AS cnt",
                    parameters={"dsid": data_subject_id},
                    database=domain_id,
                )
                node_count = count_result[0]["cnt"] if count_result else 0
            except Exception:
                node_count = -1
            # RU-066 follow-up: the dry run also scopes the packet step — the
            # requesting tenant's PacketStore rows gdpr_erase_subject would
            # delete. None when PacketStore is disabled (packets not erasable,
            # as in the real summary); -1 when the count fails, like graph_nodes.
            packet_count: int | None = None
            if _gdpr_s.packet_store_enabled:
                try:
                    from engine.packet.packet_store import get_packet_store

                    dry_pool = await get_packet_store().get_pool()
                    packet_count = await pii_handler.count_subject_packets(data_subject_id, dry_pool, tenant=tenant)
                except Exception:
                    logger.exception("GDPR dry-run packet-store count failed")
                    packet_count = -1
            return {
                "status": "dry_run",
                "data_subject_id": data_subject_id,
                "dry_run": True,
                "would_affect": {"graph_nodes": node_count, "packets": packet_count},
            }

        # Real erasure. RU-066 / DEC-26: packets live in the PacketStore database,
        # so its pool is used when PacketStore is enabled; otherwise packets are
        # reported as not erasable. The erasure is audited in the domain's
        # ComplianceEngine trail before anything is deleted, and that flush is
        # strict (RU-066 follow-up): a missing audit pool or a failed flush
        # raises here, so nothing is deleted without a durable audit entry —
        # compliance_audit_soft_sink no longer lets the delete proceed.
        try:
            packet_pool = None
            if _gdpr_s.packet_store_enabled:
                from engine.packet.packet_store import get_packet_store

                packet_pool = await get_packet_store().get_pool()
            erasure_audit = _get_compliance_engine(spec)
            erasure_audit.log_pii_erasure(
                tenant=tenant,
                data_subject_id=data_subject_id,
                detail=f"Erasure requested for subject in domain {domain_id} (property {subject_property})",
            )
            await erasure_audit.flush_audit_before_erasure()
            result = await pii_handler.erase_subject(
                data_subject_id=data_subject_id,
                graph_driver=graph_driver,
                db_pool=packet_pool,
                # DB-STRICT-01: the same domain database the dry-run count above
                # queries; the driver no longer falls back to 'neo4j'.
                database=domain_id,
                subject_property=subject_property,
                performed_by=tenant,
                # RU-066 follow-up: packets are erased for this tenant only.
                tenant=tenant,
            )
        except Exception as exc:
            raise ExecutionError(
                "GDPR erasure failed",
                action="admin",
                tenant=tenant,
                detail=str(exc),
            ) from exc

        return {
            "status": "erased",
            "data_subject_id": data_subject_id,
            "dry_run": False,
            "summary": result,
        }

    # --- W6-03: GDS Job History Exposure ---
    if subaction == "gds_status":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        scheduler = _get_or_create_scheduler(spec, graph_driver)
        history = await scheduler.get_job_history()
        # Summarize per-job last run (prefer job name, fallback to algorithm)
        last_runs: dict[str, Any] = {}
        for entry in reversed(history):
            job_key = entry.get("job") or entry.get("algorithm", "unknown")
            if job_key not in last_runs:
                last_runs[job_key] = {
                    "last_run": entry.get("timestamp"),
                    "status": entry.get("status"),
                }
        return {
            "status": "ok",
            "domain_id": domain_id,
            "last_runs": last_runs,
            "history_count": len(history),
            "recent_history": history[-10:] if len(history) > 10 else history,
        }

    if subaction == "gds_trigger":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        job_name = _require_key(payload, "job_name", "admin", tenant)
        spec = domain_loader.load_domain(domain_id)
        # Validate algorithm name exists in domain spec
        valid_jobs = {j.name for j in spec.gdsjobs}
        if job_name not in valid_jobs:
            raise ValidationError(
                f"Job '{job_name}' not found in domain spec. Available: {sorted(valid_jobs)}",
                action="admin",
                tenant=tenant,
            )
        scheduler = _get_or_create_scheduler(spec, graph_driver)
        result = await scheduler.trigger_job(job_name)
        return {"status": "triggered", "job": job_name, "result": result}

    if subaction == "gds_health":
        from datetime import UTC, datetime

        from engine.gds.staleness import StalenessPolicy, job_staleness, parse_history_timestamp

        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        scheduler = _get_or_create_scheduler(spec, graph_driver)
        history = await scheduler.get_job_history()
        policy = StalenessPolicy.from_settings()
        jobs = {job.name: job for job in spec.gdsjobs}
        now = datetime.now(tz=UTC)
        algo_health: dict[str, Any] = {}
        # Build per-job health from most recent entry (prefer job name, fallback to algorithm)
        for entry in reversed(history):
            algo = entry.get("job") or entry.get("algorithm", "unknown")
            if algo in algo_health:
                continue
            ts_str = entry.get("timestamp")
            verdict = job_staleness(jobs.get(algo), parse_history_timestamp(ts_str), now, policy)
            algo_health[algo] = {
                "last_run": ts_str,
                "status": entry.get("status"),
                "stale": verdict.stale,
                "stale_after": verdict.deadline.isoformat() if verdict.deadline else None,
            }
        # Check for algorithms that have never run
        for job in spec.gdsjobs:
            if job.name not in algo_health:
                algo_health[job.name] = {"last_run": None, "status": "never_run", "stale": True, "stale_after": None}
        all_healthy = all(not v.get("stale") for v in algo_health.values())
        return {
            "status": "healthy" if all_healthy else "degraded",
            "domain_id": domain_id,
            "max_staleness_hours": int(policy.max_staleness.total_seconds() // 3600),
            "staleness_per_schedule": policy.per_schedule,
            "staleness_grace_minutes": int(policy.grace.total_seconds() // 60),
            "algorithms": algo_health,
        }

    # --- W6-04: Feature Status ---
    if subaction == "feature_status":
        from engine.config.settings import settings as _fs

        return {
            "status": "ok",
            "feature_gates": {
                # --- Phase 4 / KGE ---
                "kge_enabled": _fs.kge_enabled,
                # --- GDS ---
                "gds_enabled": _fs.gds_enabled,
                # --- Pareto ---
                "pareto_enabled": _fs.pareto_enabled,
                "pareto_weight_discovery_enabled": _fs.pareto_weight_discovery_enabled,
                # --- Wave 1: Invariant Hardening ---
                "domain_strict_validation": _fs.domain_strict_validation,
                "score_clamp_enabled": _fs.score_clamp_enabled,
                "strict_null_gates": _fs.strict_null_gates,
                "param_strict_mode": _fs.param_strict_mode,
                # --- Wave 7: Explicit Tenant Database Binding (DB-STRICT-01) ---
                "strict_tenant_database": _fs.strict_tenant_database,
                "tenant_allowlist_required_in_prod": _fs.tenant_allowlist_required_in_prod,
                # --- Persistence (PERSIST-01) ---
                "packet_store_enabled": _fs.packet_store_enabled,
                # --- Health commercial model (HEALTH-02 / COMM-01 / COMM-02) ---
                "health_commercial_mode_enabled": _fs.health_commercial_mode_enabled,
                "health_entitled_tier": _fs.health_entitled_tier,
                # --- Wave 2: Refinement Scoring ---
                "feedback_enabled": _fs.feedback_enabled,
                "confidence_check_enabled": _fs.confidence_check_enabled,
                "score_normalize": _fs.score_normalize,
                # --- Wave 6: Dormant Feature Activation ---
                "gdpr_erasure_enabled": _fs.gdpr_erasure_enabled,
                "gdpr_dry_run": _fs.gdpr_dry_run,
                "gds_max_staleness_hours": _fs.gds_max_staleness_hours,
                "gds_staleness_per_schedule_enabled": _fs.gds_staleness_per_schedule_enabled,
                # --- Constellation seam (EIE <-> Gate <-> CEG) ---
                "graph_inference_feedback_enabled": _fs.graph_inference_feedback_enabled,
                "health_api_enabled": _fs.health_api_enabled,
                "auto_create_domain_database": _fs.auto_create_domain_database,
                # --- Domain pack admission (generic; the engine names no domain) ---
                "unvalidated_domain_packs_enabled": _fs.unvalidated_domain_packs_enabled,
                "dormant_domain_packs_enabled": _fs.dormant_domain_packs_enabled,
                # --- CEG-ENRICH-GATE: local enrich property writer ---
                "enrich_local_writer_enabled": _fs.enrich_local_writer_enabled,
            },
        }

    # --- HGKR: Evaluation Endpoint ---
    if subaction == "evaluate":
        return await _handle_evaluate(tenant, payload, graph_driver, domain_loader)

    # --- S2-08: Auto-generate calibration pairs from outcomes ---
    if subaction == "generate_calibration_pairs":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)

        # Read recent outcomes with scores (outcome node label is the domain's)
        outcome_label = _outcome_label(spec, tenant, "admin", "admin generate_calibration_pairs")
        outcome_cypher = f"""
        MATCH (o:{outcome_label})
        WHERE o.tenant = $tenant AND o.outcome IN ['success', 'failure']
        RETURN o.match_id AS match_id, o.candidate_id AS candidate_id,
               o.outcome AS outcome, o.value AS score
        ORDER BY o.created_at DESC
        LIMIT 500
        """
        try:
            outcome_records = await graph_driver.execute_query(
                cypher=outcome_cypher,
                parameters={"tenant": tenant},
                database=spec.domain.id,
            )
        except Exception as exc:
            raise ExecutionError(
                "Failed to read outcomes for calibration pair generation",
                action="admin",
                tenant=tenant,
                detail=str(exc),
            ) from exc

        from engine.scoring.hgkr_utils import generate_calibration_pairs

        pairs = generate_calibration_pairs(list(outcome_records))
        return {
            "status": "calibration_pairs_generated",
            "total_pairs": len(pairs),
            "pairs": [
                {
                    "node_a": p.node_a,
                    "node_b": p.node_b,
                    "expected_range": [p.expected_score_min, p.expected_score_max],
                    "label": p.label,
                }
                for p in pairs
            ],
        }

    # --- S2-21: Domain density report ---
    if subaction == "domain_density_report":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)

        # Query graph statistics
        stats_cypher = """
        MATCH (n)
        WITH count(n) AS total_nodes
        OPTIONAL MATCH ()-[r]-()
        WITH total_nodes, count(r) / 2 AS total_edges
        RETURN total_nodes, total_edges
        """
        try:
            stats = await graph_driver.execute_query(
                cypher=stats_cypher,
                parameters={},
                database=spec.domain.id,
            )
        except Exception as exc:
            raise ExecutionError(
                "Failed to read graph statistics",
                action="admin",
                tenant=tenant,
                detail=str(exc),
            ) from exc

        total_nodes = stats[0]["total_nodes"] if stats else 0
        total_edges = stats[0]["total_edges"] if stats else 0

        from engine.scoring.hgkr_utils import DensityReport, generate_density_report

        density_report: DensityReport = generate_density_report(
            domain_id=domain_id,
            total_nodes=total_nodes,
            total_edges=total_edges,
        )
        return {
            "status": "density_report",
            "domain_id": density_report.domain_id,
            "total_nodes": density_report.total_nodes,
            "total_edges": density_report.total_edges,
            "avg_degree": density_report.avg_degree,
            "density_class": density_report.density_class,
            "recommended_sample_size": density_report.recommended_sample_size,
            "recommendations": density_report.recommendations,
        }

    # --- S2-23: Auto-tune aggregation strategies ---
    if subaction == "auto_tune":
        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)

        from engine.scoring.hgkr_utils import build_ablation_configs

        configs = build_ablation_configs(spec.gdsjobs)
        return {
            "status": "auto_tune_configs_generated",
            "domain_id": domain_id,
            "total_configs": len(configs),
            "configs": configs,
            "instructions": (
                "Run 'evaluate' subaction with each config's gds_overrides "
                "to compare AUC across aggregation strategies. Select the "
                "config with highest AUC and apply to domain spec."
            ),
        }

    # --- Milestone 2.2: Pareto Weight Discovery ---
    if subaction == "discover_weights":
        from chassis.errors import FeatureNotEnabled
        from engine.config.settings import settings as _pw_settings

        if not _pw_settings.pareto_weight_discovery_enabled:
            raise FeatureNotEnabled(
                "Pareto Weight Discovery",
                flag="PARETO_WEIGHT_DISCOVERY_ENABLED",
                action="admin",
                tenant=tenant,
            )

        domain_id = _bind_admin_domain(tenant, payload, required=True)
        spec = domain_loader.load_domain(domain_id)
        n_samples = int(payload.get("n_samples", 50))

        if not spec.decision_arbitration or not spec.decision_arbitration.enabled:
            raise ValidationError(
                "Decision arbitration not enabled in domain spec",
                action="admin",
                tenant=tenant,
            )

        dimension_names = [obj.dimension for obj in spec.decision_arbitration.pareto_config.objectives]

        if not dimension_names:
            raise ValidationError(
                "No Pareto objectives defined in decision_arbitration.pareto_config",
                action="admin",
                tenant=tenant,
            )

        # Read outcome history from Neo4j (outcome node label is the domain's)
        outcome_label = _outcome_label(spec, tenant, "admin", "admin discover_weights")
        outcome_cypher = f"""
        MATCH (o:{outcome_label})
        WHERE o.tenant = $tenant AND o.dimension_scores IS NOT NULL
        RETURN o.dimension_scores AS dimension_scores,
               o.was_selected AS was_selected
        ORDER BY o.created_at DESC
        LIMIT 1000
        """
        try:
            raw_outcomes = await graph_driver.execute_query(
                cypher=outcome_cypher,
                parameters={"tenant": tenant},
                database=spec.domain.id,
            )
        except Exception as exc:
            raise ExecutionError(
                "Failed to read outcomes for weight discovery",
                action="admin",
                tenant=tenant,
                detail=str(exc),
            ) from exc

        # Convert to the format expected by discover_pareto_weights
        import json as _json

        outcome_history: list[dict[str, Any]] = []
        for rec in raw_outcomes:
            ds = rec.get("dimension_scores")
            if isinstance(ds, str):
                try:
                    ds = _json.loads(ds)
                except (ValueError, TypeError):
                    continue
            if not isinstance(ds, dict):
                continue
            # A stored outcome with a non-numeric dimension score is malformed;
            # skip it like the unparseable-JSON case above.
            try:
                dimension_scores = {k: float(v) for k, v in ds.items()}
            except (ValueError, TypeError):
                continue
            outcome_history.append(
                {
                    "dimension_scores": dimension_scores,
                    "was_selected": bool(rec.get("was_selected", False)),
                }
            )

        if len(outcome_history) < 10:
            return {
                "status": "insufficient_data",
                "error": "Insufficient outcome history for weight discovery",
                "min_required": 10,
                "current_count": len(outcome_history),
            }

        from engine.scoring.weight_discovery import adaptive_weight_discovery

        weight_vectors = await adaptive_weight_discovery(
            dimension_names=dimension_names,
            outcome_history=outcome_history,
            n_samples=n_samples,
        )

        return {
            "status": "weights_discovered",
            "discovered_weights": [
                {
                    "weights": wv.weights,
                    "ndcg_score": round(wv.ndcg_score, 6),
                    "diversity_score": round(wv.diversity_score, 6),
                    "coverage_score": round(wv.coverage_score, 6),
                }
                for wv in weight_vectors
            ],
            "front_size": len(weight_vectors),
            "outcome_history_size": len(outcome_history),
        }

    # ── CEG-006 / HEALTH-01: AI-readiness health, reachable from an inbound packet
    # engine/health/api.py implemented three handlers and was imported by
    # nothing (CEG-006). The trigger that once dispatched enrichment from here
    # moved to EIE (operator decision 2026-11-15). The canonical route is the
    # advertised `health-assess` action (handle_health_assess; operator decision
    # 2026-10-22). The subactions stay on `admin` as the operator surface so a
    # direct dev call keeps working; both routes share _dispatch_health.
    if subaction in _HEALTH_SUBACTIONS:
        return await _dispatch_health(tenant, payload, subaction, action="admin")

    # ── EIE-008: hand graph-derived field values back to EIE through Gate ────
    # EIE advertises and implements `graph-inference-result` end to end and no
    # code here ever produced one, so the feedback loop the architecture implies
    # had a consumer and no producer. Flag-gated: it spends EIE budget.
    if subaction == "emit_inference_feedback":
        from engine.config.settings import settings as _inf_settings

        if not _inf_settings.graph_inference_feedback_enabled:
            return {
                "status": "disabled",
                "message": ("Graph inference feedback is disabled. Set GRAPH_INFERENCE_FEEDBACK_ENABLED=True."),
            }

        from engine.gate_egress import emit_graph_inference_result
        from engine.inference_rule_registry import (
            InferenceContext,
            authorized_rules,
            execute_rule,
        )

        entity = _require_key(payload, "entity", "admin", tenant)
        entity_id = _require_key(payload, "entity_id", "admin", tenant)
        domain_id = _bind_admin_domain(tenant, payload, required=False, default=tenant)

        # The domain owns which inference may run for it (plugins.inference_rules).
        # `rules` absent means "every rule the domain authorizes"; `rules: []`
        # means "none" and must not silently become "all"; a rule the domain did
        # not authorize is rejected rather than executed. A non-list is rejected
        # rather than iterated — a bare string would otherwise be walked
        # character by character and every char looked up as a rule name.
        inference_spec = domain_loader.load_domain(domain_id)
        authorized = authorized_rules(inference_spec)
        if "rules" in payload:
            requested = payload["rules"]
            if not isinstance(requested, list) or not all(isinstance(r, str) for r in requested):
                raise ValidationError(
                    "admin.rules must be a list of rule-name strings",
                    action="admin",
                    tenant=tenant,
                )
            unauthorized = sorted(set(requested) - set(authorized))
            if unauthorized:
                raise ValidationError(
                    f"admin.rules {unauthorized} are not authorized by domain {domain_id!r} "
                    "(declare them under plugins.inference_rules in the domain spec)",
                    action="admin",
                    tenant=tenant,
                )
        else:
            requested = list(authorized)

        context = InferenceContext(
            tenant_id=tenant,
            domain_id=domain_id,
            pass_number=int(payload.get("pass_number", 1)),
            known_fields=dict(entity),
            # The domain's declared vocabulary for rules that need one (infer_buyer_persona).
            rule_parameters=dict(inference_spec.plugins.inference_rule_parameters) if inference_spec.plugins else {},
        )
        # Named `inferred`, not `result`: `result` is already bound to a
        # dict[str, Any] earlier in this function, and reusing it here is a type
        # error mypy catches (and did).
        results = [
            inferred for rule_name in requested if (inferred := execute_rule(rule_name, entity, context)) is not None
        ]
        dispatch = await emit_graph_inference_result(
            tenant=tenant,
            entity_id=str(entity_id),
            results=results,
        )
        return {
            "status": "inference_feedback_emitted",
            "entity_id": entity_id,
            "rules_run": list(requested),
            "results_produced": len(results),
            "dispatch": dispatch,
        }

    raise ValidationError(f"Unknown admin subaction: {subaction!r}", action="admin", tenant=tenant)


async def _handle_evaluate(
    tenant: str,
    payload: dict[str, Any],
    graph_driver: GraphDriver,
    domain_loader: DomainPackLoader,
) -> dict[str, Any]:
    """Evaluate scoring quality against a labeled test set.

    Computes precision@K, recall@K, F1@K, and NDCG@K for a batch
    of test cases run through handle_match.

    Payload:
        - domain_id: str — domain to evaluate against
        - test_set: list[dict] — each with "query", "match_direction",
          "expected_ids" (list[str] of known-good candidate entity_ids)
        - k: int — top-K for evaluation (default 10)
        - weights: dict — optional scoring weight overrides
        - ablation: dict — optional dimension ablation overrides
    """
    import math

    domain_id = _bind_admin_domain(tenant, payload, required=True)
    test_set = _require_key(payload, "test_set", "admin", tenant)
    k = int(payload.get("k", 10))
    weight_overrides = payload.get("weights", {})

    if not isinstance(test_set, list) or len(test_set) == 0:
        raise ValidationError(
            "test_set must be a non-empty list",
            action="admin",
            tenant=tenant,
        )

    from engine.config.settings import settings as _eval_settings

    # RU-002: read each returned candidate's declared id for the case direction
    # (`entity_id` only, without loading the spec, while the flag is off).
    eval_spec = domain_loader.load_domain(domain_id) if _eval_settings.spec_declared_candidate_id else None

    def eval_id_property(direction: str) -> str:
        if eval_spec is None:
            return _FALLBACK_CANDIDATE_ID_PROPERTY
        labels = [c.label for c in eval_spec.matchentities.candidate if c.matchdirection == direction]
        return _candidate_id_property(eval_spec, labels[0]) if labels else _FALLBACK_CANDIDATE_ID_PROPERTY

    per_case_results: list[dict[str, Any]] = []
    total_precision = 0.0
    total_recall = 0.0
    total_ndcg = 0.0

    for case_idx, case in enumerate(test_set):
        query = case.get("query", {})
        match_direction = case.get("match_direction", "")
        expected_ids = set(case.get("expected_ids", []))

        if not expected_ids:
            per_case_results.append({"case_index": case_idx, "skipped": True, "reason": "no expected_ids"})
            continue

        match_payload: dict[str, Any] = {
            "query": query,
            "match_direction": match_direction,
            "top_n": k,
        }
        if weight_overrides:
            match_payload["weights"] = weight_overrides

        try:
            # RU-003: with the domain bound to the tenant, match runs as the tenant.
            match_result = await handle_match(
                tenant if _eval_settings.admin_tenant_bound_domain else domain_id, match_payload
            )
        except Exception as exc:
            per_case_results.append({"case_index": case_idx, "error": str(exc)})
            continue

        candidates = match_result.get("candidates", [])
        returned_ids = []
        for c in candidates[:k]:
            if isinstance(c, dict):
                cand_node = c.get("candidate", {})
                if isinstance(cand_node, dict):
                    returned_ids.append(_candidate_id(cand_node, eval_id_property(match_direction), ""))
                else:
                    returned_ids.append("")
            else:
                returned_ids.append("")

        # Precision@K: fraction of returned candidates that are relevant
        relevant_returned = sum(1 for rid in returned_ids if rid in expected_ids)
        precision = relevant_returned / len(returned_ids) if returned_ids else 0.0

        # Recall@K: fraction of relevant candidates that were returned
        recall = relevant_returned / len(expected_ids) if expected_ids else 0.0

        # NDCG@K: normalized discounted cumulative gain
        dcg = 0.0
        for rank, rid in enumerate(returned_ids, start=1):
            if rid in expected_ids:
                dcg += 1.0 / math.log2(rank + 1)
        # Ideal DCG: all relevant items at top positions
        ideal_count = min(len(expected_ids), k)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg = dcg / idcg if idcg > 0 else 0.0

        total_precision += precision
        total_recall += recall
        total_ndcg += ndcg

        per_case_results.append(
            {
                "case_index": case_idx,
                "precision_at_k": round(precision, 4),
                "recall_at_k": round(recall, 4),
                "ndcg_at_k": round(ndcg, 4),
                "returned_count": len(returned_ids),
                "relevant_returned": relevant_returned,
            }
        )

    evaluated_count = sum(1 for r in per_case_results if "precision_at_k" in r)
    avg_precision = total_precision / evaluated_count if evaluated_count > 0 else 0.0
    avg_recall = total_recall / evaluated_count if evaluated_count > 0 else 0.0
    avg_ndcg = total_ndcg / evaluated_count if evaluated_count > 0 else 0.0
    f1 = (2 * avg_precision * avg_recall / (avg_precision + avg_recall)) if (avg_precision + avg_recall) > 0 else 0.0

    return {
        "status": "evaluation_complete",
        "k": k,
        "test_cases": len(test_set),
        "evaluated": evaluated_count,
        "aggregate": {
            "precision_at_k": round(avg_precision, 4),
            "recall_at_k": round(avg_recall, 4),
            "f1_at_k": round(f1, 4),
            "ndcg_at_k": round(avg_ndcg, 4),
        },
        "per_case": per_case_results,
    }


async def _init_schema(driver: GraphDriver, spec: DomainSpec) -> int:
    """Create Neo4j constraints and indexes from ontology."""
    db = spec.domain.id
    count = 0
    for node in spec.ontology.nodes:
        label = sanitize_label(node.label)
        for prop in node.properties:
            if prop.required:
                cypher = (
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{sanitize_label(prop.name)} IS NOT NULL"
                )
                try:
                    await driver.execute_query(cypher, database=db)
                    count += 1
                except Exception as exc:
                    logger.warning("Constraint failed for %s.%s: %s", label, prop.name, exc)
        id_props = [p for p in node.properties if p.required and "id" in p.name.lower()]
        for prop in id_props:
            cypher = f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{sanitize_label(prop.name)} IS UNIQUE"
            try:
                await driver.execute_query(cypher, database=db)
                count += 1
            except Exception as exc:
                logger.warning("Uniqueness constraint failed %s.%s: %s", label, prop.name, exc)
    return count


def _get_or_create_scheduler(spec: DomainSpec, driver: GraphDriver) -> GDSScheduler:
    state = get_state()
    domain_id = spec.domain.id
    if domain_id not in state.gds_schedulers:
        from engine.config.settings import settings as _gds_reg_settings

        scheduler = GDSScheduler(spec, driver)
        scheduler.register_jobs()
        # RU-007 / CEG-COR-016: boot registers its schedulers here too, so this
        # branch only runs for a domain boot did not start. With GDS disabled the
        # jobs are registered for manual trigger and no cron scheduler starts.
        if not _gds_reg_settings.gds_shared_scheduler_registry or _gds_reg_settings.gds_enabled:
            scheduler.start()
        state.gds_schedulers[domain_id] = scheduler
    result: GDSScheduler = state.gds_schedulers[domain_id]
    return result


# RU-006 / CEG-COR-015 (R1a-DEC-02): the sync `outcome` entity type used a
# second vocabulary. It maps onto the canonical success|failure|partial that
# handle_outcomes writes and every established reader counts.
_SYNC_OUTCOME_VOCABULARY: dict[str, str] = {
    "positive": "success",
    "negative": "failure",
    "neutral": "partial",
    "success": "success",
    "failure": "failure",
    "partial": "partial",
}


def _resulted_in_permitted(domain_spec: DomainSpec, record: dict[str, Any]) -> bool:
    """R3 / CEG-FIX-001: may the RESULTED_IN edge for this outcome be created?

    A rejection by CausalEdgeRuntimeValidator suppresses the edge (when
    enforcement is on); the outcome node is the observation and is still written.
    """
    if not domain_spec.causal.enabled:
        return True
    from engine.causal.causal_validator import CausalEdgeRuntimeValidator
    from engine.config.settings import settings as _causal_settings

    validator = CausalEdgeRuntimeValidator(domain_spec.causal)
    edge_record = {
        "edge_type": "RESULTED_IN",
        "source_ts": record.get("source_ts"),
        "target_ts": record.get("target_ts"),
        "confidence": record.get("confidence"),
    }
    _valid, rejected = validator.validate_batch([edge_record], "RESULTED_IN")
    if rejected:
        logger.info(
            "Causal validation rejected RESULTED_IN edge: %s",
            rejected[0].get("rejection_reason"),
        )
        return not _causal_settings.causal_rejection_enforcement_enabled
    return True


def _outcome_feedback_fields(fingerprint: dict[str, Any]) -> tuple[str | None, bool | None]:
    """RU-006: optional ``fingerprint.dimension_scores`` / ``was_selected``.

    ``dimension_scores`` is a map of dimension name to a finite number, stored
    as sorted-key JSON (RU-005 codec) because Neo4j cannot hold a map property;
    ``was_selected`` is a bool. Either absent is stored as null. Raises
    ValueError on any other shape.
    """
    import math

    from engine.models.outcomes import encode_outcome_map

    raw_scores = fingerprint.get("dimension_scores")
    encoded: str | None = None
    if raw_scores is not None:
        if not isinstance(raw_scores, dict):
            msg = "fingerprint.dimension_scores must be a map of dimension -> number"
            raise ValueError(msg)
        for dim, score in raw_scores.items():
            numeric = isinstance(score, (int, float)) and not isinstance(score, bool)
            if not isinstance(dim, str) or not numeric or not math.isfinite(score):
                msg = f"fingerprint.dimension_scores[{dim!r}] must be a finite number"
                raise ValueError(msg)
        encoded = encode_outcome_map(raw_scores)
    was_selected = fingerprint.get("was_selected")
    if was_selected is not None and not isinstance(was_selected, bool):
        msg = "fingerprint.was_selected must be a boolean"
        raise ValueError(msg)
    return encoded, was_selected


def _build_outcome_write(
    domain_spec: DomainSpec,
    tenant: str,
    *,
    outcome_id: str,
    match_id: Any,
    candidate_id: Any,
    outcome: str,
    value: Any,
    fingerprint: Any,
    create_resulted_in: bool,
    keyed: bool = False,
) -> tuple[str, dict[str, Any]]:
    """The one outcome-record writer (handle_outcomes; sync ``outcome`` under RU-006).

    Writes the domain's ``feedbackloop.outcome_node_label`` node with the match
    fingerprint and a RESULTED_IN edge from the candidate found by its declared
    id (RU-002). With ``outcome_schema_unified`` the node also carries
    ``dimension_scores`` (JSON) and ``was_selected`` for score_feedback and
    discover_weights. Raises ValueError for a malformed fingerprint.
    """
    from engine.config.settings import settings as _outcome_settings
    from engine.models.outcomes import encode_outcome_map

    unified = bool(_outcome_settings.outcome_schema_unified)
    if unified and not isinstance(fingerprint, dict):
        msg = "fingerprint must be a map"
        raise ValueError(msg)
    active_dimensions = fingerprint.get("active_dimensions", [])
    dimension_weights = fingerprint.get("dimension_weights", {})
    gates_passed = fingerprint.get("gates_passed", [])
    match_direction = fingerprint.get("match_direction")
    candidate_count = fingerprint.get("candidate_count", 0)
    feedback_fields = _outcome_feedback_fields(fingerprint) if unified else None

    # The outcome node label is the domain's (feedbackloop.outcome_node_label);
    # a pack that declares none is refused here, before any Cypher (Stage 2
    # follow-up), on both the outcomes and the sync-outcome path.
    outcome_label = _outcome_label(domain_spec, tenant, "outcomes", "an outcome write")
    feedback_props = (
        ",\n        dimension_scores: $dimension_scores,\n        was_selected: $was_selected" if unified else ""
    )
    cypher = (
        f"\n    CREATE (o:{outcome_label} {{"
        """
        outcome_id: $outcome_id, match_id: $match_id,
        candidate_id: $candidate_id, outcome: $outcome,
        value: $value, created_at: datetime(), tenant: $tenant,
        domain_id: $domain_id,
        active_dimensions: $active_dimensions,
        dimension_weights: $dimension_weights,
        gates_passed: $gates_passed,
        match_direction: $match_direction,
        candidate_count: $candidate_count"""
        f"{feedback_props}"
        """
    })
    WITH o
    OPTIONAL MATCH (c {entity_id: $candidate_id})
    FOREACH (_ IN CASE WHEN c IS NOT NULL AND $create_resulted_in THEN [1] ELSE [] END |
        CREATE (c)-[:RESULTED_IN]->(o)
    )
    RETURN o.outcome_id AS outcome_id
    """
    )

    if keyed:
        cypher = _keyed_outcome_write_cypher(outcome_label, unified)

    # RU-005 / CEG-COR-056: one decodable encoding (JSON) for the map-valued
    # fingerprint field; off restores the Python repr writer.
    stored_dimension_weights = (
        encode_outcome_map(dimension_weights) if _outcome_settings.outcome_map_json_encoding else str(dimension_weights)
    )

    # RU-002: link the outcome to the candidate by its declared id property,
    # scoped to the fingerprint's direction when it names a declared one.
    outcome_candidate_predicate = _declared_id_predicate(domain_spec, "c", "candidate_id", match_direction)
    if outcome_candidate_predicate is not None:
        cypher = cypher.replace(
            "OPTIONAL MATCH (c {entity_id: $candidate_id})",
            f"OPTIONAL MATCH (c) WHERE {outcome_candidate_predicate}",
        )
    parameters: dict[str, Any] = {
        "outcome_id": outcome_id,
        "match_id": match_id,
        "candidate_id": candidate_id,
        "outcome": outcome,
        "value": value,
        "tenant": tenant,
        "domain_id": domain_spec.domain.id,
        "active_dimensions": active_dimensions,
        "dimension_weights": stored_dimension_weights,
        "gates_passed": gates_passed,
        "match_direction": match_direction,
        "candidate_count": candidate_count,
        "create_resulted_in": create_resulted_in,
    }
    if keyed:
        parameters["request_token"] = uuid.uuid4().hex
    if feedback_fields is not None:
        parameters["dimension_scores"], parameters["was_selected"] = feedback_fields
    return cypher, parameters


_OUTCOME_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
_OUTCOME_STORED_FIELDS = ("match_id", "candidate_id", "outcome", "tenant")


def _validated_outcome_id(value: Any, tenant: str) -> str:
    """A caller-supplied outcome_id: the replay key (FEEDBACK-IDEMP-01)."""
    if not isinstance(value, str) or not _OUTCOME_ID_PATTERN.match(value):
        raise ValidationError(
            "outcome_id must be 1-128 characters of letters, digits, '_', '.', ':' or '-', starting alphanumeric",
            action="outcomes",
            tenant=tenant,
        )
    return value


def _outcome_id_constraint_query(outcome_label: str) -> str:
    """Uniqueness of ``outcome_id`` on the domain's outcome label (idempotent).

    MERGE alone cannot stop two concurrent first writes of one key both
    creating a node; the constraint makes the database refuse the second.
    A schema statement, so it runs in its own transaction, never with the write.
    """
    label = sanitize_label(outcome_label)
    return f"CREATE CONSTRAINT ceg_outcome_id_{label} IF NOT EXISTS FOR (o:{label}) REQUIRE o.outcome_id IS UNIQUE"


def _keyed_outcome_write_cypher(outcome_label: str, unified: bool) -> str:
    """MERGE form of the outcome write for a caller-supplied outcome_id.

    The node is created once; a replay matches it and changes nothing.
    ``created`` is true only for the transaction whose per-request token was
    written ON CREATE (the token is removed before commit). RESULTED_IN is only
    created with the node. The stored identity fields are returned so the
    handler can refuse a replay whose content differs.
    """
    label = sanitize_label(outcome_label)
    fields = [
        "match_id",
        "candidate_id",
        "outcome",
        "value",
        "tenant",
        "domain_id",
        "active_dimensions",
        "dimension_weights",
        "gates_passed",
        "match_direction",
        "candidate_count",
    ]
    if unified:
        fields += ["dimension_scores", "was_selected"]
    assignments = ",\n        ".join(f"o.{name} = ${name}" for name in fields)
    stored = ", ".join(f"o.{name} AS stored_{name}" for name in _OUTCOME_STORED_FIELDS)
    return f"""
    MERGE (o:{label} {{outcome_id: $outcome_id}})
    ON CREATE SET
        {assignments},
        o.created_at = datetime(),
        o._created_by_request = $request_token
    WITH o, coalesce(o._created_by_request = $request_token, false) AS created
    REMOVE o._created_by_request
    WITH o, created
    OPTIONAL MATCH (c {{entity_id: $candidate_id}})
    FOREACH (_ IN CASE WHEN created AND c IS NOT NULL AND $create_resulted_in THEN [1] ELSE [] END |
        CREATE (c)-[:RESULTED_IN]->(o)
    )
    RETURN DISTINCT o.outcome_id AS outcome_id, created, {stored}
    """


def _keyed_outcome_replayed(
    rows: list[dict[str, Any]],
    expected: dict[str, Any],
    tenant: str,
) -> bool:
    """Whether a keyed outcome write matched an existing node (a replay).

    A replay whose identity fields differ from the stored outcome is refused:
    one outcome_id names one observation.
    """
    if not rows:
        raise ExecutionError("Outcome write returned no row", action="outcomes", tenant=tenant)
    row = rows[0]
    if row.get("created"):
        return False
    conflicts = sorted(name for name in _OUTCOME_STORED_FIELDS if row.get(f"stored_{name}") != expected.get(name))
    if conflicts:
        raise ValidationError(
            f"outcome_id {expected['outcome_id']!r} is already recorded with different {', '.join(conflicts)}",
            action="outcomes",
            tenant=tenant,
        )
    return True


async def handle_outcomes(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write the domain's outcome node (feedbackloop.outcome_node_label) with match fingerprint and RESULTED_IN edge."""
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, "outcomes")

    match_id = _require_key(payload, "match_id", "outcomes", tenant)
    candidate_id = _require_key(payload, "candidate_id", "outcomes", tenant)
    outcome = _require_key(payload, "outcome", "outcomes", tenant)

    if outcome not in ("success", "failure", "partial"):
        raise ValidationError(
            f"Invalid outcome: {outcome!r}. Must be success|failure|partial.",
            action="outcomes",
            tenant=tenant,
        )

    domain_spec = domain_loader.load_domain(tenant)
    _enforce_capability(tenant, "outcomes", domain_spec)
    from engine.config.settings import settings as _idempotency_settings

    # FEEDBACK-IDEMP-01: a caller-supplied outcome_id is the replay key.
    replay_key = payload.get("outcome_id")
    keyed = bool(_idempotency_settings.outcome_idempotency_enabled) and replay_key is not None
    outcome_id = _validated_outcome_id(replay_key, tenant) if keyed else f"out_{uuid.uuid4().hex[:12]}"

    # R3: Causal validation for RESULTED_IN edge. CEG-FIX-001: a rejection
    # suppresses the edge in both write paths below; the outcome node is the
    # observation and is still recorded.
    create_resulted_in = _resulted_in_permitted(domain_spec, payload)

    # R5: Extract match fingerprint from payload
    fingerprint = payload.get("fingerprint", {})
    try:
        cypher, parameters = _build_outcome_write(
            domain_spec,
            tenant,
            outcome_id=outcome_id,
            match_id=match_id,
            candidate_id=candidate_id,
            outcome=outcome,
            value=payload.get("value"),
            fingerprint=fingerprint,
            create_resulted_in=create_resulted_in,
            keyed=keyed,
        )
    except ValueError as exc:
        raise ValidationError(str(exc), action="outcomes", tenant=tenant) from exc
    if keyed:
        try:
            await graph_driver.execute_query(
                cypher=_outcome_id_constraint_query(
                    _outcome_label(domain_spec, tenant, "outcomes", "an outcome write")
                ),
                database=domain_spec.domain.id,
            )
        except Exception as exc:
            raise ExecutionError(
                "Outcome write could not enforce outcome_id uniqueness; nothing written",
                action="outcomes",
                tenant=tenant,
                detail=str(exc),
            ) from exc
    try:
        write_rows = await graph_driver.execute_query(
            cypher=cypher,
            parameters=parameters,
            database=domain_spec.domain.id,
        )
    except Exception as exc:
        raise ExecutionError(
            "Outcome write failed",
            action="outcomes",
            tenant=tenant,
            detail=str(exc),
        ) from exc
    replayed = keyed and _keyed_outcome_replayed(write_rows, parameters, tenant)

    # A replay recorded nothing new: the side effects below ran with the first
    # write. Only the feedback loop runs again; its propagation claim makes the
    # boost apply once per outcome_id and lets a retry finish a failed one.

    # R6: Counterfactual generation for negative outcomes
    if domain_spec.counterfactual.enabled and outcome == "failure" and not replayed:
        try:
            from engine.causal.counterfactual import CounterfactualGenerator

            generator = CounterfactualGenerator(
                counterfactual_spec=domain_spec.counterfactual,
                graph_driver=graph_driver,
                domain_id=domain_spec.domain.id,
                outcome_label=_outcome_label(domain_spec, tenant, "outcomes", "counterfactual generation"),  # RU-075
            )
            await generator.generate_for_outcome(outcome_id)
        except Exception:
            logger.warning("Counterfactual generation failed for %s", outcome_id, exc_info=True)

    compliance = _get_compliance_engine(domain_spec)
    if compliance.enabled and not replayed:
        compliance.log_outcome(tenant=tenant, outcome_id=outcome_id, outcome=outcome)

    response: dict[str, Any] = {"status": "recorded", "outcome_id": outcome_id}
    if replayed:
        response["replayed"] = True
        logger.info("Outcome replay tenant=%s outcome_id=%s", tenant, outcome_id)
    elif not create_resulted_in:
        response["rejected_count"] = 1

    # Feedback loop: propagate outcome to signal weights and score adjustments
    # (FEEDBACK-01: same predicate as every other feedback site).
    if _feedback_effective(domain_spec):
        from engine.feedback.convergence import ConvergenceLoop

        outcome_data = {
            "outcome_id": outcome_id,
            "match_id": match_id,
            "candidate_id": candidate_id,
            "outcome": outcome,
            "value": payload.get("value"),
        }
        loop = ConvergenceLoop(graph_driver, domain_spec)
        feedback_metadata = await loop.on_outcome_recorded(outcome_data)
        response["feedback"] = feedback_metadata

    # W2-02b: Persist outcome_jsonb to PacketStore (feedback loop substrate linkage).
    # Dual-gated: settings.outcome_persistence_enabled AND PACKET_STORE_ENABLED=true.
    # Requires payload.match_packet_id to link outcome back to originating match packet.
    # Non-fatal: PacketStore.record_outcome() never raises; always returns bool.
    from engine.config.settings import settings as _outcome_settings

    if _outcome_settings.outcome_persistence_enabled and not replayed:
        _match_packet_id = payload.get("match_packet_id")
        if _match_packet_id:
            from engine.packet.packet_store import get_packet_store as _get_store

            _feedback_meta = response.get("feedback")
            await _get_store().record_outcome(
                match_packet_id=_match_packet_id,
                tenant=tenant,
                outcome_id=outcome_id,
                outcome=outcome,
                match_id=match_id,
                candidate_id=candidate_id,
                value=payload.get("value"),
                feedback_metadata=_feedback_meta,
            )
        else:
            logger.debug(
                "W2-02b: outcome_persistence_enabled=True but "
                "payload.match_packet_id not provided -- PacketStore linkage skipped. "
                "tenant=%s outcome_id=%s",
                tenant,
                outcome_id,
            )

    # Causal edge creation: auto-create causal chain from outcome
    if domain_spec.causal.enabled and not replayed:
        from engine.causal.causal_compiler import CausalCompiler

        compiler = CausalCompiler(domain_spec)
        for edge_spec in domain_spec.causal.causal_edges:
            if edge_spec.edge_type == "RESULTED_IN" and create_resulted_in:
                # RU-068: the source is the candidate, identified like everywhere
                # else in handlers; the target (outcome) identity is resolved by
                # the compiler. The threshold is bound, never interpolated.
                edge_cypher = compiler.compile_causal_edge_create(
                    edge_spec,
                    source_id_property=_candidate_id_property(domain_spec, edge_spec.source_label),
                )
                try:
                    await graph_driver.execute_query(
                        cypher=edge_cypher,
                        parameters={
                            "source_id": candidate_id,
                            "target_id": outcome_id,
                            "confidence": 1.0,
                            "mechanism": f"outcome_{outcome}",
                            "confidence_threshold": edge_spec.confidence_threshold,
                        },
                        database=domain_spec.domain.id,
                    )
                except Exception:
                    logger.warning("Causal edge creation failed for outcome=%s", outcome_id, exc_info=True)

        # Compute attribution if enabled
        if domain_spec.causal.attribution_enabled:
            from engine.causal.attribution import AttributionCalculator

            attr_calc = AttributionCalculator(graph_driver, domain_spec)
            try:
                attribution = await attr_calc.compute_attribution(outcome_id)
                response["attribution"] = attribution
            except Exception:
                logger.warning("Attribution calculation failed for outcome=%s", outcome_id, exc_info=True)

    return response


async def handle_resolve(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Entity resolution: create RESOLVED_FROM edge between duplicate entities.

    When semantic_registry is enabled, uses multi-signal similarity scoring
    to find duplicates and link each one to a canonical entity with a
    ``(duplicate)-[:RESOLVED_FROM]->(canonical)`` edge. Otherwise, falls back
    to manual ``(source)-[:RESOLVED_FROM]->(target)`` edge creation.

    Resolution is linkage only (RU-102): no relationships are moved and match
    does not consult RESOLVED_FROM; ``merged_count`` counts links created.
    """
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, "resolve")

    entity_type = _require_key(payload, "entity_type", "resolve", tenant)
    domain_spec = domain_loader.load_domain(tenant)
    _enforce_capability(tenant, "resolve", domain_spec)

    # R8: Semantic registry-based resolution
    if domain_spec.semantic_registry.enabled and entity_type in domain_spec.semantic_registry.entity_labels:
        from engine.resolution.resolver import EntityResolver

        resolver = EntityResolver(
            registry_spec=domain_spec.semantic_registry,
            graph_driver=graph_driver,
            domain_id=domain_spec.domain.id,
        )

        # Batch mode: resolve all entities of this type
        if payload.get("mode") == "batch":
            try:
                result = await resolver.resolve_batch(
                    entity_label=entity_type,
                    threshold=payload.get("threshold"),
                )
            except Exception as exc:
                raise ExecutionError(
                    "Batch entity resolution failed",
                    action="resolve",
                    tenant=tenant,
                    detail=str(exc),
                ) from exc
            else:
                return {"status": "batch_resolved", **result}

        # Single entity mode
        source_id = _require_key(payload, "source_id", "resolve", tenant)
        try:
            result = await resolver.resolve_entity(
                entity_id=source_id,
                entity_label=entity_type,
            )
        except Exception as exc:
            raise ExecutionError(
                "Entity resolution failed",
                action="resolve",
                tenant=tenant,
                detail=str(exc),
            ) from exc
        else:
            return {"status": "resolved", **result}

    # Legacy manual resolution
    source_id = _require_key(payload, "source_id", "resolve", tenant)
    target_id = _require_key(payload, "target_id", "resolve", tenant)
    confidence = payload.get("confidence", 1.0)
    signal = payload.get("signal", "manual")

    label = sanitize_label(entity_type)
    resolution_id = f"res_{uuid.uuid4().hex[:12]}"
    # RU-002: the declared id property for this label (entity_id with the flag off).
    id_prop = sanitize_label(_candidate_id_property(domain_spec, entity_type))

    cypher = f"""
    MATCH (source:{label} {{{id_prop}: $source_id}})
    MATCH (target:{label} {{{id_prop}: $target_id}})
    CREATE (source)-[r:RESOLVED_FROM {{
        resolution_id: $resolution_id, confidence: $confidence,
        signal: $signal, created_at: datetime(), tenant: $tenant
    }}]->(target)
    RETURN r.resolution_id AS resolution_id
    """
    try:
        created = await graph_driver.execute_query(
            cypher=cypher,
            parameters={
                "source_id": source_id,
                "target_id": target_id,
                "resolution_id": resolution_id,
                "confidence": confidence,
                "signal": signal,
                "tenant": tenant,
            },
            database=domain_spec.domain.id,
        )
    except Exception as exc:
        raise ExecutionError(
            "Entity resolution failed",
            action="resolve",
            tenant=tenant,
            detail=str(exc),
        ) from exc

    from engine.config.settings import settings as _resolve_settings

    # RU-004 / CEG-COR-014: no returned row means a MATCH found nothing and no
    # RESOLVED_FROM edge exists; report it (RETURN_VALUES.md error propagation).
    if _resolve_settings.resolve_requires_created_edge and not created:
        raise ValidationError(
            f"source or target entity not found for {entity_type}",
            action="resolve",
            tenant=tenant,
        )

    return {
        "status": "resolved",
        "resolution_id": resolution_id,
        "source_id": source_id,
        "target_id": target_id,
    }


# Named status constants — never use string literals for health evaluation
_HEALTH_OK = "ok"
_HEALTH_NEO4J_CONN_FAILED = "error: connection_failed"
_HEALTH_DOMAIN_TENANT_MISSING = "ok: tenant not found; fallback available"
_HEALTH_DOMAIN_NO_PACKS = "error: no_domains_found"
_HEALTH_DOMAIN_CONFIG_INVALID = "error: config_invalid"


async def handle_health(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Health check: Neo4j connectivity and domain spec validity."""
    from engine.config.loader import DomainNotFoundError

    graph_driver, domain_loader = _require_deps()
    checks: dict[str, str] = {"neo4j": "unknown", "domain_spec": "unknown"}

    try:
        # Query the 'neo4j' default database — 'system' only accepts DDL (SHOW/CREATE DATABASE)
        await graph_driver.execute_query("RETURN 1 AS ping", database="neo4j")
        checks["neo4j"] = _HEALTH_OK
    except Exception:
        checks["neo4j"] = _HEALTH_NEO4J_CONN_FAILED

    try:
        domain_loader.load_domain(tenant)
        checks["domain_spec"] = _HEALTH_OK
    except DomainNotFoundError:
        # Tenant spec absent — check whether any domain packs are loaded at all.
        # A generic /v1/health probe (tenant="default") should not fail when the
        # deployment is healthy but the "default" tenant hasn't been seeded.
        available = domain_loader.list_domains()
        if available:
            from engine.config.settings import settings as _health_settings

            # RU-013 / CEG-COR-074: the probe tenant is arbitrary and the probe is
            # unauthenticated, so report how many packs exist, not their ids.
            if _health_settings.health_probe_domain_count_only:
                checks["domain_spec"] = f"{_HEALTH_DOMAIN_TENANT_MISSING}: {len(available)} domain packs"
            else:
                checks["domain_spec"] = f"{_HEALTH_DOMAIN_TENANT_MISSING}: {available}"
        else:
            checks["domain_spec"] = _HEALTH_DOMAIN_NO_PACKS
    except Exception:
        # Unexpected loader error — real configuration problem, not just missing tenant
        checks["domain_spec"] = _HEALTH_DOMAIN_CONFIG_INVALID

    neo4j_ok = checks["neo4j"] == _HEALTH_OK
    domain_ok = checks["domain_spec"] == _HEALTH_OK
    overall = "healthy" if (neo4j_ok and domain_ok) else "degraded"
    return {"status": overall, "checks": checks}


async def handle_healthcheck(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Alias for handle_health (spec compatibility)."""
    return await handle_health(tenant, payload)


# ── HEALTH-01: canonical `health-assess` action ──────────────────────────────
# Gate's action-ownership map gives `health-assess` to `ceg` (Constellation.Gate
# lane G3): consumer and operator keys may call it; worker keys are refused it,
# except EIE's node `enrichment-engine`, admitted for `health-assess` alone
# (HEALTH-01 amendment 2026-11-15, Constellation.Gate #43). CEG checks only Gate
# provenance, never caller kind.
# `admin` is never advertised and never routed by Gate, so the health subactions
# need a route of their own. The subaction names are kept so a payload that
# works through `admin` in dev works unchanged through Gate.
HEALTH_ASSESS_ACTION = "health-assess"
_HEALTH_SUBACTIONS: frozenset[str] = frozenset({"health_assess", "health_batch_assess", "health_report"})


async def _dispatch_health(tenant: str, payload: dict[str, Any], subaction: str, *, action: str) -> dict[str, Any]:
    """Shared body of the three health subactions (CEG-006, HEALTH-05).

    Reached from ``health-assess`` (the canonical Gate route) and from ``admin``
    (operator surface); ``action`` only labels the errors. Both callers have
    already confined ``subaction`` to ``_HEALTH_SUBACTIONS``. Order matters: the
    feature flag answers before any tenant-bound check so a disabled API reads
    as disabled, and the HEALTH-05 check runs before any domain pack is loaded.
    """
    from engine.config.settings import settings as _health_settings

    # Mechanism ships dormant, operator activates — the same contract every
    # other behavioural surface here follows. These handlers assess and report
    # only; CEG dispatches no enrichment (the trigger lives in EIE).
    if not _health_settings.health_api_enabled:
        return {
            "status": "disabled",
            "subaction": subaction,
            "message": "Health API is disabled. Set HEALTH_API_ENABLED=True.",
        }

    # HEALTH-05: tenant id == domain spec id == Neo4j database name, with no
    # mapping layer. A payload.domain naming any other spec is a cross-tenant
    # read; reject it here, before any domain pack is loaded.
    requested_domain = payload.get("domain")
    if requested_domain is not None and requested_domain != tenant:
        raise ValidationError(
            f"payload.domain {requested_domain!r} does not match packet tenant {tenant!r} "
            f"for {subaction}; health subactions read only the tenant's own domain",
            action=action,
            tenant=tenant,
        )

    from engine.health import api as health_api

    # Resolved per call, not bound at import: tests patch these on the module.
    health_handlers = {
        "health_assess": health_api.handle_health_assess,
        "health_batch_assess": health_api.handle_health_batch_assess,
        "health_report": health_api.handle_health_report,
    }
    return await health_handlers[subaction](tenant, payload)


async def handle_health_assess(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical health route (HEALTH-01): Gate action ``health-assess``.

    Dispatches exactly ``payload.subaction`` in {``health_assess``,
    ``health_batch_assess``, ``health_report``} to ``engine/health/api.py``.
    Any other subaction is refused with ``ValidationError``, so nothing else
    ``handle_admin`` offers is reachable from a Gate-routed packet: Gate routes
    ``health-assess`` and never ``admin``. Same preconditions as the admin path:
    initialised engine, tenant allowlist (CEG-AUTH-01), HEALTH-05 tenant-bound
    domain.
    """
    _require_deps()
    _validate_tenant_access(tenant, HEALTH_ASSESS_ACTION)
    subaction = _require_key(payload, "subaction", HEALTH_ASSESS_ACTION, tenant)
    if subaction not in _HEALTH_SUBACTIONS:
        raise ValidationError(
            f"subaction {subaction!r} is not a health subaction; {HEALTH_ASSESS_ACTION} dispatches only "
            f"{sorted(_HEALTH_SUBACTIONS)} and never the rest of admin",
            action=HEALTH_ASSESS_ACTION,
            tenant=tenant,
        )
    return await _dispatch_health(tenant, payload, subaction, action=HEALTH_ASSESS_ACTION)


ENRICH_LOCAL_WRITER_FLAG = "ENRICH_LOCAL_WRITER_ENABLED"


def _enrich_declared_properties(domain_spec: DomainSpec, label: str) -> set[str]:
    """Every property the ontology declares on ``label`` (empty when undeclared)."""
    return {prop.name for node in domain_spec.ontology.nodes if node.label == label for prop in node.properties}


def _enrich_sync_owned_properties(domain_spec: DomainSpec, label: str) -> frozenset[str]:
    """Node properties of ``label`` the pack assigns to ``sync`` (CEG-ENRICH-GATE).

    Ownership follows what ``engine/sync/generator.py`` actually writes:

    * An ``UNWINDMATCHSET`` endpoint that declares ``fieldsupdated`` writes only
      those fields plus ``updated_at`` (and the system ``_tenant``), matching
      on ``idproperty``; it owns exactly those, never the rest of the node.
    * Any other node endpoint targeting ``label`` (MERGE, declared-projection
      MERGE, or ``UNWINDMATCHSET`` without ``fieldsupdated``, which runs
      ``SET n += row``) owns its ``idproperty``, every property the ontology
      declares on that node, its ``fieldsupdated`` fields, and the row keys its
      taxonomy edges and childsync read.

    A childsync whose ``targetnode`` is ``label`` owns that child's
    ``targetid`` and every property declared on the child node, because sync
    writes ``c += child_row`` there. A label no endpoint writes owns nothing.
    """
    owned: set[str] = set()
    for endpoint in domain_spec.sync.endpoints if domain_spec.sync else []:
        if endpoint.targetnode == label and not endpoint.targetedge:
            owned |= _enrich_node_endpoint_owned(domain_spec, label, endpoint)
        if any(child.targetnode == label for child in endpoint.childsync or []):
            owned |= _enrich_declared_properties(domain_spec, label)
            owned.update(child.targetid for child in endpoint.childsync or [] if child.targetnode == label)
    return frozenset(owned)


def _enrich_node_endpoint_owned(domain_spec: DomainSpec, label: str, endpoint: SyncEndpointSpec) -> set[str]:
    """Properties one node sync endpoint targeting ``label`` writes (see the caller's rules)."""
    if endpoint.batchstrategy == SyncStrategy.UNWINDMATCHSET and endpoint.fieldsupdated:
        owned = {*endpoint.fieldsupdated, "updated_at"}
    else:
        owned = _enrich_declared_properties(domain_spec, label)
        owned.update(endpoint.fieldsupdated or [])
        owned.update(edge.field for edge in endpoint.taxonomyedges or [])
        owned.update(child.field for child in endpoint.childsync or [])
    if endpoint.idproperty:
        owned.add(endpoint.idproperty)
    return owned


def _enrich_refuse_owned(tenant: str, label: str, props: list[str], owned: frozenset[str]) -> None:
    """Refuse, before any Cypher runs, an enrichment of a sync-owned or system property."""
    system = sorted({p for p in props if p.startswith("_")})
    if system:
        msg = f"enrich may not write system properties {system} on {label!r}; they are sync/engine owned"
        raise ValidationError(msg, action="enrich", tenant=tenant)
    clash = sorted(set(props) & owned)
    if clash:
        msg = (
            f"enrich may not write {clash} on {label!r}: a sync endpoint of the pack owns them "
            "and scoring reads the sync-written value"
        )
        raise ValidationError(msg, action="enrich", tenant=tenant)


async def handle_enrich(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Enrich action: add computed properties to graph entities.

    CEG-ENRICH-GATE: dormant behind ``enrich_local_writer_enabled``. Off, the
    call raises ``FeatureNotEnabled`` before any spec load or graph access. On,
    a property any node sync endpoint of the target label owns (or any
    ``_``-prefixed system property) is refused before any Cypher runs.

    Payload schema:
      - entity_type: str       # Node label to enrich
      - entity_ids: list[str]  # Optional: specific entities (all if omitted)
      - enrichments: list[dict] # Each: {property: str, expression: str}

    Returns:
      - enriched_count: int
      - entity_type: str
      - tenant: str
    """
    from engine.config.settings import settings as _enrich_settings

    if not _enrich_settings.enrich_local_writer_enabled:
        logger.info("enrich refused for tenant %s: %s is false", tenant, ENRICH_LOCAL_WRITER_FLAG)
        raise FeatureNotEnabled(
            "Local enrich property writer",
            flag=ENRICH_LOCAL_WRITER_FLAG,
            action="enrich",
            tenant=tenant,
        )
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, "enrich")
    domain_spec = domain_loader.load_domain(tenant)
    _enforce_capability(tenant, "enrich", domain_spec)

    entity_type = payload.get("entity_type")
    if not entity_type:
        raise ValidationError("entity_type required", action="enrich", tenant=tenant)

    entity_ids = payload.get("entity_ids", [])
    enrichments = payload.get("enrichments", [])

    if not enrichments:
        return {"status": "success", "enriched_count": 0, "entity_type": entity_type, "tenant": tenant}

    label = sanitize_label(entity_type)

    set_clauses = []
    safe_props: list[str] = []
    for enrich in enrichments:
        prop = enrich.get("property", "")
        expr = enrich.get("expression", "")
        if prop and expr:
            safe_prop = sanitize_label(prop)
            safe_expr = _sanitize_expression(expr)
            safe_props.append(safe_prop)
            set_clauses.append(f"n.{safe_prop} = {safe_expr}")

    _enrich_refuse_owned(tenant, label, safe_props, _enrich_sync_owned_properties(domain_spec, label))

    if not set_clauses:
        return {"status": "success", "enriched_count": 0, "entity_type": entity_type, "tenant": tenant}

    if entity_ids:
        # RU-002: filter on the label's declared id property (entity_id with the flag off).
        id_prop = sanitize_label(_candidate_id_property(domain_spec, entity_type))
        cypher = f"""
        MATCH (n:{label})
        WHERE n.{id_prop} IN $entity_ids
        SET {", ".join(set_clauses)}
        RETURN count(n) AS enriched_count
        """
        params = {"entity_ids": entity_ids}
    else:
        cypher = f"""
        MATCH (n:{label})
        SET {", ".join(set_clauses)}
        RETURN count(n) AS enriched_count
        """
        params = {}

    result = await graph_driver.execute_query(
        cypher=cypher,
        parameters=params,
        database=domain_spec.domain.id,
    )

    count = result[0]["enriched_count"] if result else 0
    return {"status": "success", "enriched_count": count, "entity_type": entity_type, "tenant": tenant}


SCORE_ACTION = "score"
_SCORE_READER_BLOCKED = (
    "no evidence binding supplied: a local caller passes a GraphEvidenceBinding with a "
    "server-approved relevance map; the remote route is handle_score_remote"
)


async def handle_score(
    tenant: str,
    payload: dict[str, Any],
    *,
    binding: EvidenceBinding | None = None,
) -> dict[str, Any]:
    """Local ``graph_evidence_priority_v1`` cohort scoring (QM.POC0.CEG.SCORE.001).

    Deliberately NOT in ``ACTION_HANDLERS``: the Gate-routed ``score`` action
    is ``handle_score_remote``, which checks activation and the tenant's
    ``score:read`` grant and then calls this with the binding it built from the
    admitted spec (SCORING-GATE-01). It loads no domain pack and calls
    nothing on the match path. ``tenant`` comes from the caller's context only;
    by HEALTH-05 it is also the Neo4j database the binding may read. The request
    is validated in full before any read. With no ``binding``, or one that
    cannot read its graph (``EvidenceUnavailableError``: absent database, no
    connection), nothing is scored and the result is ``blocked``. The Neo4j
    binding is ``engine.scoring.graph_evidence.GraphEvidenceBinding``, built by
    the local caller with a server-approved relevance map.
    """
    _validate_tenant_access(tenant, SCORE_ACTION)
    try:
        request = parse_score_request(payload)
    except ScoreRequestError as exc:
        raise ValidationError(str(exc), action=SCORE_ACTION, tenant=tenant) from exc
    defect = tenant_id_defect(tenant)
    if defect is not None:
        raise ValidationError(
            f"tenant {defect}; it names the database a score read is confined to",
            action=SCORE_ACTION,
            tenant=tenant,
        )
    if binding is None:
        return blocked_result(request, _SCORE_READER_BLOCKED)
    try:
        read = await binding.read(database=tenant, entity_refs=request.entity_refs)
    except EvidenceUnavailableError as exc:
        return blocked_result(request, str(exc))
    if read.database != tenant:
        raise ExecutionError(
            f"evidence binding read database {read.database!r}, not the tenant database {tenant!r}",
            action=SCORE_ACTION,
            tenant=tenant,
        )
    from engine.config.settings import settings as _score_settings

    return score_cohort(
        request,
        read,
        binding.approved_relevance,
        identity_per_type=_score_settings.evidence_identity_per_type_enabled,
    )


def _require_score_grant(tenant: str, domain_spec: DomainSpec) -> None:
    """Explicit tenant ``score:read`` (SCORING-GATE-01 §1.3), whatever CAPABILITY_AUTH_ENABLED says.

    ``_enforce_capability`` is not used: it returns when the global switch is off
    or the spec declares no capabilities. Here a missing declaration, a missing
    ``score:read`` entry, a wildcard or another tenant's grant all deny.
    """
    cap_set = _build_capability_set(domain_spec)
    if cap_set is None or not cap_set.grants_explicitly(tenant, PERM_SCORE_READ):
        msg = f"Tenant '{tenant}' holds no explicit '{PERM_SCORE_READ}' grant; a wildcard never grants score"
        logger.warning("score refused: %s", msg)
        raise AuthorizationError(msg, action=SCORE_ACTION, tenant=tenant)


async def handle_score_remote(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Gate-routed ``score`` (SCORING-GATE-01): the secured adapter onto ``handle_score``.

    ``tenant`` is the SDK transport's, which GATE-KEY-01 binds to a Gate
    signature. Every refusal happens before the binding exists, so a refused
    call reads no graph: ``remote_score_enabled`` off, typed evidence identity
    off, an unauthorised tenant, a malformed request, no explicit
    ``score:read``, or a spec with no approved relevance or no declared evidence
    type. The binding is built here from the admitted spec and the injected
    driver; nothing in the payload reaches it. ``handle_score`` and the
    algorithm are unchanged.
    """
    from engine.config.settings import settings as _score_settings

    # The SDK returns only the error type to the caller; these lines are the operator's diagnosis.
    if not _score_settings.remote_score_enabled:
        logger.info("score refused for tenant %s: REMOTE_SCORE_ENABLED is false", tenant)
        raise FeatureNotEnabled("Remote score", flag="REMOTE_SCORE_ENABLED", action=SCORE_ACTION, tenant=tenant)
    if not _score_settings.evidence_identity_per_type_enabled:
        logger.warning("score refused for tenant %s: EVIDENCE_IDENTITY_PER_TYPE_ENABLED is false", tenant)
        raise FeatureNotEnabled(
            "Remote score typed evidence identity",
            flag="EVIDENCE_IDENTITY_PER_TYPE_ENABLED",
            message=(
                "remote score requires EVIDENCE_IDENTITY_PER_TYPE_ENABLED=true: relationship sync writes "
                "(relationship_type, evidence_ref) identity and a ref-only rank is refused"
            ),
            action=SCORE_ACTION,
            tenant=tenant,
        )
    graph_driver, domain_loader = _require_deps()
    _validate_tenant_access(tenant, SCORE_ACTION)
    defect = tenant_id_defect(tenant)
    if defect is not None:
        raise ValidationError(f"tenant {defect}", action=SCORE_ACTION, tenant=tenant)
    try:
        request = parse_score_request(payload)
    except ScoreRequestError as exc:
        raise ValidationError(str(exc), action=SCORE_ACTION, tenant=tenant) from exc
    domain_spec = domain_loader.load_domain(tenant)
    _require_score_grant(tenant, domain_spec)
    relevance = approved_relevance_from_spec(domain_spec)
    evidence_types = declared_evidence_types(domain_spec)
    if not relevance or not evidence_types:
        logger.info("score blocked for tenant %s: spec has no approved relevance or evidence type", tenant)
        return blocked_result(
            request,
            "tenant spec is not admissible for scoring: it needs approved evidencerelevance at "
            "-reviewed/-production and at least one declared targetedge evidence type",
        )
    binding = GraphEvidenceBinding(graph_driver, relevance, relationship_types=evidence_types)
    return await handle_score(tenant, payload, binding=binding)


# CONTRACT-02: single source of truth for the engine's action surface.
# Both chassis implementations (legacy chassis/actions.py and the SDK-native
# chassis/handler_registration.py) route off this dict rather than each
# maintaining their own action list, so the two chassis can't drift apart.
def _relationship_row_defect(check: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Why one relationship row cannot be written, from its precheck result, else None."""
    if check["from_any"] > check["from_count"] or check["to_any"] > check["to_count"]:
        return "an endpoint id resolves to a node outside this tenant"
    if check["from_count"] != 1 or check["to_count"] != 1:
        return (
            f"endpoints must each resolve to exactly one existing node "
            f"(from: {check['from_count']}, to: {check['to_count']})"
        )
    for existing in check["existing"]:
        same = (
            existing["from_id"] == row["from_id"]
            and existing["to_id"] == row["to_id"]
            and stored_evidence(existing["props"]) == row["props"]
        )
        if not same:
            return (
                "evidence_ref is already recorded with different endpoints or evidence; provenance is not overwritten"
            )
    return None


class _RelationshipNotRecordedError(ExpectedWriteOutcome):
    """The write did not record exactly the submitted evidence.

    Raised inside the managed write transaction so the driver rolls the whole
    batch back; converted to ``ExecutionError`` outside it.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _relationship_defects(checks: list[dict[str, Any]], by_ref: dict[str, dict[str, Any]]) -> tuple[list[str], int]:
    """Per-row refusals from the precheck, and how many rows replay an identical stored edge."""
    defects = []
    replayed = 0
    for check in checks:
        row = by_ref[check["evidence_ref"]]
        defect = _relationship_row_defect(check, row)
        if defect is not None:
            defects.append(f"{check['evidence_ref']}: {defect}")
        elif check["existing"]:
            replayed += 1
    return sorted(defects), replayed


RelationshipWrite = Callable[[Any], Awaitable[tuple[list[str], int]]]


async def _run_relationship_write(
    graph_driver: GraphDriver, write: RelationshipWrite, database: str, tenant: str
) -> tuple[list[str], int]:
    """One managed write transaction; a constraint failure propagates for the caller to classify."""
    try:
        # The callable form returns whatever the transaction function returns.
        result = cast("tuple[list[str], int]", await graph_driver.execute_write(write, database=database))
    except ConstraintError:
        raise
    except _RelationshipNotRecordedError as exc:
        raise ExecutionError(
            "Relationship sync did not record the submitted evidence; rolled back",
            action="sync",
            tenant=tenant,
            detail=exc.detail,
        ) from exc
    except Exception as exc:
        raise ExecutionError("Relationship sync failed", action="sync", tenant=tenant, detail=str(exc)) from exc
    return result


async def _write_relationships_once_retried(
    graph_driver: GraphDriver, write: RelationshipWrite, database: str, tenant: str
) -> tuple[list[str], int]:
    """Run the relationship write transaction; retry once if a concurrent writer won the identity race.

    The uniqueness constraint refuses the losing commit (rolled back whole).
    The retry's precheck then sees the winner's edge: an identical assertion
    replays, a different one is refused. A second constraint failure means the
    evidence_ref is held by an edge the precheck cannot see (the same type
    between other labels), which is a refusal, not an outage.
    """
    try:
        return await _run_relationship_write(graph_driver, write, database, tenant)
    except ConstraintError:
        logger.info("Relationship sync lost an evidence identity race in %s; re-checking once", database)
    try:
        return await _run_relationship_write(graph_driver, write, database, tenant)
    except ConstraintError as exc:
        raise ValidationError(
            f"Relationship sync refused, nothing written: an evidence_ref is already recorded ({exc.code})",
            action="sync",
            tenant=tenant,
        ) from exc


async def _sync_relationships(
    graph_driver: GraphDriver,
    domain_spec: DomainSpec,
    endpoint_spec: SyncEndpointSpec,
    *,
    entity_type: str,
    batch: list[Any],
    tenant: str,
) -> dict[str, Any]:
    """Relationship evidence sync for a `targetedge` endpoint (QM.POC0.CEG.GRAPH_EVIDENCE.002A).

    All-or-nothing: the declaration and every row are validated before any
    graph I/O; then the precheck, the write and the post-write comparison run in
    ONE managed write transaction, so a refused or unrecorded batch leaves the
    graph unchanged. The tenant-scoped ``(type, evidence_ref)`` identity is a
    database uniqueness constraint, so two concurrent writers can never both
    commit one ``evidence_ref``: the loser's commit fails, and its single retry
    re-reads the winner's edge and answers replay or conflict like any other.
    Endpoints must already exist under this tenant; no node is created here.
    """
    try:
        declaration = resolve_relationship_declaration(domain_spec, endpoint_spec)
        rows = normalize_relationship_rows(batch)
    except ValueError as exc:
        raise ValidationError(str(exc), action="sync", tenant=tenant) from exc

    database = domain_spec.domain.id
    params = {"batch": rows, "tenant": tenant, "schema": RELATIONSHIP_EVIDENCE_SCHEMA}
    by_ref = {row["evidence_ref"]: row for row in rows}
    precheck = relationship_precheck_query(declaration)
    merge = relationship_merge_query(declaration)

    try:
        # Schema statements cannot share a transaction with data writes.
        await graph_driver.execute_query(
            cypher=relationship_uniqueness_constraint_query(declaration), database=database
        )
    except Exception as exc:
        raise ExecutionError(
            "Relationship sync could not enforce evidence identity (the per-type uniqueness "
            "constraint requires Neo4j Enterprise 5.7+); nothing written",
            action="sync",
            tenant=tenant,
            detail=str(exc),
        ) from exc

    async def _write(tx: Any) -> tuple[list[str], int]:
        checks = await (await tx.run(precheck, params)).data()
        defects, replayed = _relationship_defects(checks, by_ref)
        if defects:
            # Nothing has been written; the empty transaction commits.
            return defects, 0
        written = await (await tx.run(merge, params)).data()
        mismatched = sorted(
            entry["evidence_ref"]
            for entry in written
            if stored_evidence(entry["stored"]) != by_ref[entry["evidence_ref"]]["props"]
        )
        if mismatched or len(written) != len(rows):
            detail = f"mismatched={mismatched} written={len(written)} expected={len(rows)}"
            raise _RelationshipNotRecordedError(detail)
        return [], replayed

    defects, replayed = await _write_relationships_once_retried(graph_driver, _write, database, tenant)
    if defects:
        raise ValidationError(
            f"Relationship sync refused, nothing written: {'; '.join(defects)}",
            action="sync",
            tenant=tenant,
        )
    return {
        "status": "success",
        "entity_type": entity_type,
        "synced_count": len(batch),
        "created_count": len(rows) - replayed,
        "replayed_count": replayed,
    }


ActionHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

ACTION_HANDLERS: dict[str, ActionHandler] = {
    "match": handle_match,
    "sync": handle_sync,
    "admin": handle_admin,
    "outcomes": handle_outcomes,
    "resolve": handle_resolve,
    "health": handle_health,
    "healthcheck": handle_healthcheck,
    "health-assess": handle_health_assess,
    "enrich": handle_enrich,
    SCORE_ACTION: handle_score_remote,
}


def register_all(chassis_router: Any) -> None:
    """Register every action handler with a legacy chassis router interface.

    The primary registration path is via chassis.actions._init_engine()
    which consumes ACTION_HANDLERS directly. This function exists for
    chassis implementations that use a router.register_handler() pattern.
    """
    for action, handler in ACTION_HANDLERS.items():
        chassis_router.register_handler(action, handler)
    logger.info(
        "Registered %d action handlers: %s",
        len(ACTION_HANDLERS),
        ", ".join(ACTION_HANDLERS),
    )
