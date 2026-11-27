"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, domain-loader]
owner: engine-team
status: active
--- /L9_META ---

Domain pack loader.
Discovers, validates, caches, and hot-reloads domain spec YAML files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import yaml
from pydantic import ConfigDict, Field, RootModel, model_validator, with_config
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import TypedDict

from engine.config.schema import DomainActivation, DomainSpec, GateSpec, ScoringDimensionSpec
from engine.config.settings import settings
from engine.utils.security import tenant_id_defect

if TYPE_CHECKING:
    from engine.scoring.assembler import ScoringAssembler

logger = logging.getLogger(__name__)

MAX_SPEC_BYTES = 5 * 1024 * 1024
SPEC_FILENAME = "spec.yaml"

# Cypher parameter names the compiler may emit. Anything else (`$85.0`, `$5`)
# is a scalar `queryparam` that GateSpec coerced to a string and the compiler
# would hand to Neo4j as a parameter *name*.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A `$` reference in compiled Cypher, captured up to the next whitespace or
# closing punctuation. The capture must stop at `}` / `]` / `,` / `;` as well as
# `)`: the traversal-gate target filter form `{prop: $param}` would otherwise
# yield `param}` and withhold a valid pack for a misreported reason (PR #318
# review, verified on Neo4j 5 in the Stage 4 proof).
_PARAM_REF = re.compile(r"\$([^\s)}\],;]+)")


class DomainNotFoundError(Exception):
    """Raised when a requested domain spec does not exist or is withheld."""


class DomainSpecError(Exception):
    """Raised when a domain spec fails validation."""


@with_config(ConfigDict(extra="forbid", strict=True))
class _CurrentRecord(TypedDict):
    schema: Literal["ceg.domain-current/v1"]
    tenant_id: str
    version: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


@with_config(ConfigDict(extra="forbid", strict=True))
class _ApprovalRecord(TypedDict):
    schema: Literal["ceg.domain-approval/v1"]
    tenant_id: str
    version: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    reviewed_input_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    reviewed_by: Annotated[str, Field(min_length=1, max_length=200)]
    approval_ref: Annotated[str, Field(min_length=1, max_length=1000)]
    reviewed_at: str
    previous_sha256: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")]


class DomainCurrent(RootModel[_CurrentRecord]):
    """Atomic native JSON pointer with strict literal-key validation and no aliases."""


class DomainApproval(RootModel[_ApprovalRecord]):
    """Unsigned native review assertion; root fields preserve canonical JSON keys."""

    @staticmethod
    def explicit_review(value: str) -> str:
        if not value.strip() or value != value.strip() or any(ord(char) < 32 for char in value):
            msg = "reviewer and approval reference must be explicit nonblank text"
            raise ValueError(msg)
        return value

    @staticmethod
    def review_timestamp(value: str) -> str:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = "reviewed_at must be an ISO-8601 timezone-aware timestamp"
            raise ValueError(msg) from exc
        if timestamp.tzinfo is None or timestamp > datetime.now(tz=UTC):
            msg = "reviewed_at must be timezone-aware and not in the future"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_review(self) -> DomainApproval:
        self.explicit_review(self.root["reviewed_by"])
        self.explicit_review(self.root["approval_ref"])
        self.review_timestamp(self.root["reviewed_at"])
        return self


class ServedRevision(TypedDict):
    """Issue #400 F4: identity of the exact spec revision a tenant is served.

    Captured at admission from the bytes the loader actually admitted — never
    recomputed from a second read. Carries no path and no spec content.
    ``source`` is ``receipt`` (the revision digest verified against its
    receipt) or ``plain`` (sha256 of the ``spec.yaml`` bytes loaded).
    ``stage`` is the ``domain.version`` suffix, or ``None`` when unsuffixed.
    """

    tenant_id: str
    sha256: str
    version: str
    stage: str | None
    source: Literal["receipt", "plain"]


_VERSION_STAGE = re.compile(r"-(seed|discovered|inferred|proposed|reviewed|production)$")


def version_stage(version: str) -> str | None:
    """Stage suffix of an admitted ``domain.version``; ``None`` when it has none."""
    found = _VERSION_STAGE.search(version)
    return found.group(1) if found else None


def _served_revision(spec: DomainSpec, sha256: str, source: Literal["receipt", "plain"]) -> ServedRevision:
    return {
        "tenant_id": spec.domain.id,
        "sha256": sha256,
        "version": spec.domain.version,
        "stage": version_stage(spec.domain.version),
        "source": source,
    }


def read_native_file(path: Path, *, limit: int = MAX_SPEC_BYTES) -> bytes:
    """Read one bounded regular file, rejecting symlinks in every path component.

    Directory-fd traversal prevents a parent symlink swap between check/open.
    Each immutable spec is hashed and parsed from these same bytes.
    """
    absolute = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise DomainSpecError(f"Native input must be a regular file: {path}")
            if info.st_size > limit:
                raise DomainSpecError(f"Domain spec exceeds maximum size: {info.st_size} bytes > {limit} bytes")
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise DomainSpecError(f"Domain spec exceeds maximum size: {limit} bytes")
            return data
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise DomainSpecError(f"Cannot read native input (missing, unreadable or symlink): {path}: {exc}") from exc
    finally:
        os.close(fd)


class _NativeSafeLoader(yaml.SafeLoader):
    """Safe YAML with unambiguous explicit keys; template merge aliases preserved."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise DomainSpecError(f"Invalid YAML: duplicate or non-string mapping key {key!r}")
            seen.add(key)
        self.flatten_mapping(node)
        return cast("dict[Any, Any]", super().construct_mapping(node, deep=deep))


def _bounded_yaml(text: str) -> Any:
    """Bound alias-expanded size/depth before constructing an untrusted YAML DAG."""
    loader = _NativeSafeLoader(text)
    try:
        node = loader.get_single_node()
        visiting: set[int] = set()
        counts: dict[int, int] = {}

        def count(item: Any, depth: int = 0) -> int:
            if item is None:
                return 0
            identity = id(item)
            if identity in visiting or depth > 100:
                raise DomainSpecError("Invalid YAML: cyclic aliases or excessive nesting")
            if identity in counts:
                return counts[identity]
            visiting.add(identity)
            children = (
                [child for pair in item.value for child in pair]
                if isinstance(item, yaml.MappingNode)
                else item.value
                if isinstance(item, yaml.SequenceNode)
                else []
            )
            total = 1
            for child in children:
                total += count(child, depth + 1)
                if total > 100_000:
                    raise DomainSpecError("Invalid YAML: alias-expanded structure exceeds limit")
            visiting.remove(identity)
            counts[identity] = total
            return total

        count(node)
        return loader.construct_document(node) if node is not None else None
    finally:
        loader.dispose()


def _parse_native_bytes(
    data: bytes,
    tenant_id: str | None = None,
    *,
    complete: bool = False,
    require_reviewed: bool = False,
) -> DomainSpec:
    """Shared bounded parsing, schema, identity and stage checks; not admission."""
    if len(data) > MAX_SPEC_BYTES:
        raise DomainSpecError(f"Domain spec exceeds maximum size: {len(data)} bytes > {MAX_SPEC_BYTES} bytes")
    try:
        text = data.decode("utf-8")
        raw = _bounded_yaml(text)
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise DomainSpecError(f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise DomainSpecError(f"Domain spec must be a YAML mapping, got {type(raw).__name__}")
    if complete:
        required_guards = (
            "domain_strict_validation",
            "domain_version_semver_enforced",
            "gate_spec_forbid_unknown_keys",
            "gate_name_uniqueness",
            "domain_undeclared_param_rejected",
            "weight_ceiling_includes_unscoped",
            "traversal_open_range_rejected",
            "prohibited_factor_full_coverage",
            "gate_role_exemption",
        )
        disabled = [name for name in required_guards if not getattr(settings, name)]
        if disabled:
            raise DomainSpecError(f"Complete native admission requires validation guards: {', '.join(disabled)}")
        unknown = sorted(set(raw) - set(DomainSpec.model_fields))
        if unknown:
            raise DomainSpecError(f"Complete native admission refuses unknown top-level keys: {unknown}")
    try:
        spec = DomainSpec.model_validate(raw)
    except (PydanticValidationError, RecursionError) as exc:
        raise DomainSpecError(f"Domain '{tenant_id}' validation failed:\n{exc}") from exc
    defect = tenant_id_defect(spec.domain.id)
    if defect is not None:
        raise DomainNotFoundError(f"Invalid domain ID: {defect}")
    if tenant_id is not None and spec.domain.id != tenant_id:
        raise DomainSpecError(
            f"Domain '{tenant_id}' declares domain.id {spec.domain.id!r}; the directory name is the lookup key"
        )
    if require_reviewed or (settings.is_production and settings.domain_production_stage_required):
        require_reviewed_stage(spec)
    return spec


def validate_native_bytes(
    data: bytes,
    tenant_id: str | None = None,
    *,
    complete: bool = False,
    require_reviewed: bool = False,
) -> DomainSpec:
    """Sole runtime/publisher admission surface; behavioral defaults unchanged.

    Complete operator admission checks traversal/scoring regardless of runtime
    withholding overrides, without rewriting settings or reviewed bytes.
    Authoring compatibility is a separate policy over the same native parser.
    """
    spec = _parse_native_bytes(data, tenant_id, complete=complete, require_reviewed=require_reviewed)
    reason = withholding_reason(spec, complete=complete)
    if reason is not None:
        raise DomainNotFoundError(reason)
    return spec


def require_reviewed_stage(spec: DomainSpec) -> None:
    """Native publication never promotes/relabels an unsuffixed or partial draft."""
    semver = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    if re.fullmatch(rf"{semver}-(reviewed|production)", spec.domain.version) is None:
        raise DomainSpecError("Native publication stage must be reviewed or production")


def validate_native_file(
    path: Path,
    tenant_id: str | None = None,
    *,
    complete: bool = False,
    require_reviewed: bool = False,
    receipt_path: Path | None = None,
) -> DomainSpec:
    """Read once and delegate all admission decisions to native bytes admission."""
    data = read_native_file(path)
    spec = validate_native_bytes(
        data,
        tenant_id,
        complete=complete or receipt_path is not None,
        require_reviewed=require_reviewed or receipt_path is not None,
    )
    if receipt_path is not None:
        validate_native_approval(data, spec, read_native_record(receipt_path, DomainApproval))
    return spec


def validate_authoring_file(
    path: Path,
    tenant_id: str | None = None,
    *,
    strict: bool = False,
    require_reviewed: bool = False,
) -> tuple[list[dict[str, str]], int]:
    """Predecessor default/strict authoring checks, independent of runtime bypasses.

    This diagnostic is not runtime admission or operator approval. Its global
    scoring-weight ceiling intentionally differs from runtime's per-direction
    schema ceiling. Native compilers/validators remain the semantic owners.
    """
    spec = _parse_native_bytes(read_native_file(path), tenant_id, require_reviewed=require_reviewed)
    results = _authoring_cross_reference_results(spec)
    results.extend(_authoring_gate_results(spec))
    try:
        if spec.traversal is None:
            results.append({"check": "W1-04-traversal", "status": "PASS", "message": "No traversal defined"})
        else:
            for direction, warnings in _native_traversal_warnings(spec):
                if warnings:
                    results.extend({"check": "W1-04-traversal", "status": "WARN", "message": w} for w in warnings)
                else:
                    results.append(
                        {
                            "check": "W1-04-traversal",
                            "status": "PASS",
                            "message": f"Traversal valid for direction '{direction}'",
                        }
                    )
    except Exception as exc:
        results.append({"check": "W1-04-traversal", "status": "FAIL", "message": f"Traversal validation failed: {exc}"})
    try:
        _validate_native_compliance(spec, scoring=False)
        results.append(
            {"check": "W3-02-prohibited-factors", "status": "PASS", "message": "No prohibited factors in gates"}
        )
    except Exception as exc:
        results.append(
            {"check": "W3-02-prohibited-factors", "status": "FAIL", "message": f"Prohibited factor violation: {exc}"}
        )
    failed = any(result["status"] == "FAIL" for result in results)
    warned = any(result["status"] == "WARN" for result in results)
    return results, 1 if failed or (strict and warned) else 2 if warned else 0


def _authoring_cross_reference_results(spec: DomainSpec) -> list[dict[str, str]]:
    """Unconditional W1-01 authoring checks, including the legacy global ceiling."""
    results: list[dict[str, str]] = []
    node_labels = {node.label for node in spec.ontology.nodes}
    edge_types = {edge.type for edge in spec.ontology.edges}
    for edge in spec.ontology.edges:
        for end, label, field in (("source", edge.from_, "from"), ("target", edge.to, "to")):
            if label not in node_labels:
                results.append(
                    {
                        "check": f"W1-01-edge-{end}",
                        "status": "FAIL",
                        "message": f"Edge '{edge.type}' {end} '{label}' not in ontology nodes",
                        "path": f"ontology.edges[type={edge.type}].{field}",
                    }
                )
    for candidate in spec.matchentities.candidate:
        if candidate.label not in node_labels:
            results.append(
                {
                    "check": "W1-01-candidate-label",
                    "status": "FAIL",
                    "message": f"Candidate label '{candidate.label}' not in ontology",
                    "path": f"matchentities.candidate[label={candidate.label}]",
                }
            )
    for gate in spec.gates:
        if gate.edgetype and gate.edgetype not in edge_types:
            results.append(
                {
                    "check": "W1-01-gate-edge",
                    "status": "FAIL",
                    "message": f"Gate '{gate.name}' references unknown edge type '{gate.edgetype}'",
                    "path": f"gates[name={gate.name}].edgetype",
                }
            )
    weight_sum = sum(dimension.defaultweight for dimension in spec.scoring.dimensions)
    if weight_sum > 1.0 + 1e-9:
        results.append(
            {
                "check": "W1-01-weight-sum",
                "status": "FAIL",
                "message": f"Scoring default weights sum to {weight_sum:.4f}, exceeding 1.0",
                "path": "scoring.dimensions[*].defaultweight",
            }
        )
    return results or [{"check": "W1-01-cross-ref", "status": "PASS", "message": "All cross-references valid"}]


def _authoring_gate_results(spec: DomainSpec) -> list[dict[str, str]]:
    """Mandatory W1-03 direction compilation by the existing native compiler."""
    from engine.gates.compiler import GateCompiler

    results: list[dict[str, str]] = []
    try:
        compiler = GateCompiler(spec)
        for direction in spec.queryschema.matchdirections:
            clause = compiler.compile_all_gates(direction)
            if clause:
                results.append(
                    {
                        "check": "W1-03-gate-compile",
                        "status": "PASS",
                        "message": f"Gates compile for direction '{direction}' ({len(clause)} chars)",
                    }
                )
    except Exception as exc:
        results.append({"check": "W1-03-gate-compile", "status": "FAIL", "message": f"Gate compilation failed: {exc}"})
    return results or [{"check": "W1-03-gate-compile", "status": "PASS", "message": "No gates to compile"}]


def _native_traversal_warnings(spec: DomainSpec) -> list[tuple[str, list[str]]]:
    """Shared traversal owner's warnings: authoring warns, complete admission fails."""
    from engine.traversal.assembler import TraversalAssembler

    assembler = TraversalAssembler(spec)
    return [(direction, assembler.validate_traversal(direction)) for direction in spec.queryschema.matchdirections]


def _validate_native_compliance(spec: DomainSpec, *, scoring: bool) -> None:
    """Use the native prohibited-factor owner, with explicit coverage policy."""
    from engine.compliance.prohibited_factors import ProhibitedFactorValidator

    compliance = ProhibitedFactorValidator(spec)
    for gate in spec.gates:
        compliance.validate_gate(gate)
    if scoring:
        for dimension in spec.scoring.dimensions:
            compliance.validate_scoring_dimension(dimension)


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DomainSpecError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_native_record[Record: (DomainCurrent, DomainApproval)](path: Path, model: type[Record]) -> Record:
    """Bounded, strict JSON receipt/pointer parsing, including duplicate-key refusal."""
    try:
        raw = json.loads(read_native_file(path, limit=16 * 1024), object_pairs_hook=_unique_json)
        return model.model_validate(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DomainSpecError(f"Invalid native record {path}: {exc}") from exc


def validate_native_approval(data: bytes, spec: DomainSpec, receipt: DomainApproval) -> None:
    """Bind already parsed native review evidence to these exact admitted bytes."""
    digest = hashlib.sha256(data).hexdigest()
    if receipt.root["tenant_id"] != spec.domain.id or receipt.root["sha256"] != digest:
        raise DomainSpecError("Native receipt tenant/hash mismatch or spec tamper")
    if receipt.root["reviewed_input_sha256"] != digest:
        raise DomainSpecError("Native approval does not bind exact reviewed input bytes")
    if receipt.root["version"] != spec.domain.version:
        raise DomainSpecError("Native receipt version does not match admitted spec")
    if receipt.root["previous_sha256"] == digest:
        raise DomainSpecError("Native receipt cannot refer to itself as the previous revision")


def load_native_revision(root: Path, tenant_id: str, sha256: str) -> tuple[DomainSpec, DomainApproval]:
    """Admit one immutable revision and bind its exact bytes to review evidence."""
    defect = tenant_id_defect(tenant_id)
    if defect is not None or re.fullmatch(r"[a-f0-9]{64}", sha256) is None:
        raise DomainSpecError("Invalid native revision tenant or sha256")
    revision = root / tenant_id / "revisions" / sha256
    data = read_native_file(revision / SPEC_FILENAME)
    digest = hashlib.sha256(data).hexdigest()
    receipt = read_native_record(revision / "receipt.json", DomainApproval)
    if digest != sha256:
        raise DomainSpecError("Native receipt tenant/hash mismatch or spec tamper")
    spec = validate_native_bytes(data, tenant_id, complete=True, require_reviewed=True)
    validate_native_approval(data, spec, receipt)
    return spec, receipt


def admission_defects(spec: DomainSpec, *, complete: bool = False) -> list[str]:
    """Gate compilations that would fail or silently reject every candidate.

    This is the generic, domain-neutral admission check that replaced the
    former per-domain feature-flag allowlist (CEG domain-neutrality Stage 2,
    R07). The loader admits a pack only when it validates against ``DomainSpec``
    *and* every gate compiles to Cypher that can execute. Three defect shapes,
    all found when CEG-009 first made the migrated packs readable:

    * ``RELATES_TO`` — the compiler's fallback when a ``type: traversal`` gate
      declares no ``edgetype``. Packs written with ``pattern``/``condition`` hit
      it, and no ontology declares ``RELATES_TO``, so the gate is a hard filter
      that matches nothing.
    * ``strictwhen`` — declared by ``GateSpec`` and consumed by no compiler, so
      a gate meant to be conditional runs as an unconditional hard filter.
    * ``$<non-identifier>`` — a scalar ``queryparam``; since C-009 validates
      parameter names like labels the compiler refuses the gate outright, and
      that refusal is the same defect reported at compile time.

    Making a pack readable is not the same as making it correct. Reaching these
    packs needs either compiler support for pattern/condition, literal operands
    and strictwhen, or a query-schema parameter per constant — a schema
    decision, not a file move. Until then they are withheld, and
    ``settings.unvalidated_domain_packs_enabled`` is the operator's explicit,
    documented override (docs/FEATURE_GATES.md §16).
    """
    from engine.config.outcome_vocabulary import scoring_metadata_defects, undeclared_success_value_dimensions
    from engine.config.structural_vocabulary import UndeclaredStructuralPropertyError, structural_property_defects
    from engine.gates.compiler import GateCompiler

    compiler = GateCompiler(spec)
    # Stage 2 follow-up (item 8): structural properties a gate, scoring
    # dimension or GDS job uses are declarations (declared_structural_properties_required_enabled).
    defects: list[str] = structural_property_defects(spec)
    # A preference_attention dimension declares its success value
    # (preference_attention_metadata_required_enabled); reported once, here.
    undeclared_success = undeclared_success_value_dimensions(spec)
    defects.extend(scoring_metadata_defects(undeclared_success))
    for gate in spec.gates:
        if gate.strictwhen:
            defects.append(f"{gate.name}: strictwhen {gate.strictwhen!r} is not consumed by any compiler")
        defects.extend(_role_field_defects(gate))
        try:
            cypher = compiler.compile(gate)
        except UndeclaredStructuralPropertyError:
            continue  # reported above, once, by structural_property_defects
        except (ValueError, KeyError) as exc:
            defects.append(f"{gate.name}: refused by the compiler — {exc}")
            continue
        if "RELATES_TO" in cypher and not gate.edgetype:
            defects.append(f"{gate.name}: RELATES_TO fallback — {cypher}")
        for param in _PARAM_REF.findall(cypher):
            if not _IDENTIFIER.fullmatch(param):
                defects.append(f"{gate.name}: parameter {param!r} is not an identifier — {cypher}")
    defects.extend(_plugin_defects(spec))
    defects.extend(_sync_shape_defects(spec))
    if complete or settings.scoring_admission_check_enabled or settings.admission_checks_full_match:
        defects.extend(
            _scoring_defects(
                spec,
                check_producers=complete or settings.scoring_admission_check_enabled,
                reported=set(undeclared_success),
            )
        )
    if complete or settings.admission_checks_full_match:
        defects.extend(_match_defects(spec))
    if complete:
        defects.extend(_complete_native_defects(spec))
    return list(dict.fromkeys(defects))


def _plugin_defects(spec: DomainSpec) -> list[str]:
    """Inference-rule authorization and parameters (``plugins``) as admission defects."""
    plugins = spec.plugins
    if not plugins or not (plugins.inference_rules or plugins.inference_rule_parameters):
        return []
    from engine.inference_rule_registry import list_registered_rules, rule_parameter_defects

    # plugins.inference_rules authorizes registered rules by name; a name the
    # registry does not know would surface only when emit_inference_feedback
    # runs it (KeyError), so it is an admission defect, not a runtime surprise.
    registered = set(list_registered_rules())
    defects = [
        f"plugins.inference_rules: {rule_name!r} is not a registered inference rule"
        for rule_name in plugins.inference_rules
        if rule_name not in registered
    ]
    # Rule parameters (the domain's vocabulary for rules such as
    # infer_buyer_persona) are checked against the authorized rules.
    defects.extend(rule_parameter_defects(spec))
    return defects


# Prefixes of the typed sync contract's admission defects (row_contract.py).
_SYNC_SHAPE_PREFIXES = ("sync ", "ontology node ", "ontology edge ")


def _sync_shape_defects(spec: DomainSpec) -> list[str]:
    """Typed sync contract at admission (``engine/sync/row_contract.py``).

    A declared ``map`` / ``list<map>`` no childsync consumes (Neo4j cannot
    store either), and a node sync endpoint whose ``childsync`` /
    ``taxonomyedges`` / ``fieldsupdated`` fields its ``targetnode`` does not
    declare in a writable shape. Unconditional, like the tenant-mapping
    declarations (D-7): a pack whose sync contract the spec cannot state is
    withheld; ``unvalidated_domain_packs_enabled`` remains the operator override.
    """
    from engine.sync.row_contract import endpoint_shape_defects, stored_shape_defects

    defects = stored_shape_defects(spec)
    for endpoint in spec.sync.endpoints if spec.sync else []:
        defects.extend(endpoint_shape_defects(spec, endpoint))
    return defects


def _complete_native_defects(spec: DomainSpec) -> list[str]:
    """Use native traversal/compliance/sync owners for full operator admission."""
    from engine.sync.generator import SyncGenerator, resolve_relationship_declaration

    defects: list[str] = []
    edge_types = {edge.type for edge in spec.ontology.edges}
    for gate in spec.gates:
        if gate.edgetype and gate.edgetype not in edge_types:
            defects.append(f"gate {gate.name}: undeclared edge type {gate.edgetype!r}")
    for _direction, warnings in _native_traversal_warnings(spec):
        defects.extend(warnings)
    try:
        _validate_native_compliance(spec, scoring=True)
    except ValueError as exc:
        defects.append(f"compliance: {exc}")
    if spec.sync:
        generator = SyncGenerator(spec)
        nodes = {node.label: {prop.name for prop in node.properties} for node in spec.ontology.nodes}
        for endpoint in spec.sync.endpoints:
            try:
                if endpoint.targetedge:
                    resolve_relationship_declaration(spec, endpoint)
                else:
                    if endpoint.targetnode not in nodes or endpoint.idproperty not in nodes[endpoint.targetnode]:
                        defects.append(f"sync {endpoint.path}: targetnode/idproperty is not declared by ontology")
                    generator.generate_sync_query(endpoint, [])
            except (ValueError, KeyError, TypeError) as exc:
                defects.append(f"sync {endpoint.path}: refused by native sync owner — {exc}")
    return defects


def _role_field_defects(gate: GateSpec) -> list[str]:
    """RU-015 / CEG-COR-080 (R1a-DEC-05): role exemption and relaxed mode are not served.

    ``handle_match`` passes no request role to ``compile_all_gates`` and never
    calls ``compile_relaxed``, so a gate's ``roleexempt`` and ``relaxedpenalty``
    are accepted and ignored. Until a domain needs roles, a pack declaring
    either is withheld with this named defect. Gated by
    ``settings.gate_role_exemption``.
    """
    if not settings.gate_role_exemption:
        return []
    defects: list[str] = []
    if gate.roleexempt is not None:
        defects.append(f"{gate.name}: roleexempt {gate.roleexempt!r} is not consumed — match takes no request role")
    if "relaxedpenalty" in gate.model_fields_set:
        defects.append(
            f"{gate.name}: relaxedpenalty {gate.relaxedpenalty!r} is not consumed — relaxed matching is not exposed"
        )
    return defects


def _match_defects(spec: DomainSpec) -> list[str]:
    """Per declared match direction: the traversal assembles and the gate WHERE binds.

    ``handle_match`` assembles ``TraversalAssembler.assemble_traversal`` and
    ``GateCompiler.compile_all_gates`` for the requested direction and sends
    the result with the parameters it binds; a step the assembler refuses or a
    ``$parameter`` nothing binds fails every match in that direction. Scoring
    is covered by ``_scoring_defects``. No Neo4j access.
    """
    from engine.gates.compiler import GateCompiler
    from engine.traversal.assembler import TraversalAssembler

    defects: list[str] = []
    bindable = _bindable_parameters(spec)
    for direction in spec.queryschema.matchdirections:
        cypher_parts: list[str] = []
        try:
            cypher_parts.extend(TraversalAssembler(spec).assemble_traversal(direction))
        except (ValueError, KeyError) as exc:
            defects.append(f"traversal[{direction}]: refused by the traversal assembler — {exc}")
        compiler = GateCompiler(spec)
        try:
            cypher_parts.append(compiler.compile_all_gates(direction))
        except (ValueError, KeyError) as exc:
            defects.append(f"match[{direction}]: gates refused by the compiler — {exc}")
        bound = bindable | set(compiler.last_query_params)
        unbound = sorted({p for part in cypher_parts for p in _PARAM_IDENTIFIER_REF.findall(part) if p not in bound})
        if unbound:
            names = ", ".join(repr(p) for p in unbound)
            defects.append(
                f"match[{direction}]: gates or traversal reference parameter(s) {names}, which nothing binds"
            )
    return defects


# A `$name` parameter reference in compiled Cypher (identifiers only; the
# non-identifier form is the separate `$85.0` defect above).
_PARAM_IDENTIFIER_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _bindable_parameters(spec: DomainSpec) -> set[str]:
    """Parameter names ``handle_match`` always binds before executing a match.

    Every queryschema field (backfilled with its default), every derived
    parameter (``ParameterResolver``), every scoring ``queryprop`` (backfilled
    with null for the scoring expressions) and ``top_n``. Values the compilers
    bind themselves come from their ``last_query_params`` and are added by the
    caller.
    """
    names = {field.name for field in spec.queryschema.fields}
    names.update(param.name for param in spec.derivedparameters)
    names.update(dim.queryprop for dim in spec.scoring.dimensions if dim.queryprop)
    names.add("top_n")
    return names


def _no_producer_defect(dim: object, *, candidate_properties: set[str]) -> str | None:
    """A declared HGKR option whose input the engine never produces and the pack does not declare (R1b-DEC-01 A).

    A pack that declares the input is its producer: ``_prior_<dim>`` /
    ``shared_neighbor_score`` declared on a candidate node is written by sync.
    ``population_mean`` (either option) reads ``$_popmean_<dim>``, which
    ``handle_match`` binds when the pack declares it as a queryschema field or
    derived parameter; when it does not, the unbound-parameter check in
    ``_scoring_defects`` reports it, so it is not a producer defect here.
    """
    from engine.config.schema import ColdStartFallback, NullStrategy

    name = getattr(dim, "name", "?")
    strategy = getattr(dim, "null_strategy", NullStrategy.ZERO)
    if strategy is NullStrategy.INHERIT_PRIOR and f"_prior_{name}" not in candidate_properties:
        return f"scoring {name}: null_strategy inherit_prior has no producer (nothing writes candidate._prior_{name})"
    fallback = getattr(dim, "cold_start_fallback", ColdStartFallback.ZERO)
    if fallback is ColdStartFallback.STRUCTURAL_SIMILARITY and "shared_neighbor_score" not in candidate_properties:
        return (
            f"scoring {name}: cold_start_fallback structural_similarity has no producer "
            "(nothing writes candidate.shared_neighbor_score)"
        )
    return None


def _scoring_defects(spec: DomainSpec, *, check_producers: bool, reported: set[str] | None = None) -> list[str]:
    """Scoring dimensions that cannot execute in a declared match direction.

    For each direction, every applicable dimension is compiled exactly as
    ``ScoringAssembler.assemble_scoring_clause`` compiles it: a ``ValueError``
    is a defect quoting the compiler, and every ``$parameter`` the expression
    references must be one ``handle_match`` binds, or the match fails with
    ``ParameterMissing``. One defect per offending dimension. A dimension in
    ``reported`` already carries a named admission defect and is not compiled.
    """
    reported = reported or set()
    defects: list[str] = _producer_defects(spec) if check_producers else []
    bindable = _bindable_parameters(spec)
    for direction in spec.queryschema.matchdirections:
        defects.extend(_direction_scoring_defects(spec, direction, bindable=bindable, reported=reported, prior=defects))
    return defects


def _producer_defects(spec: DomainSpec) -> list[str]:
    """``_no_producer_defect`` for every scoring dimension, in declaration order.

    CEG-R01: the producer must be declared on the candidate of each direction
    the dimension scores (``dimension_candidate_properties``; the legacy union
    with ``scoring_spec_strict_validation_enabled`` off).
    """
    from engine.config.schema import dimension_candidate_properties

    producers = (
        _no_producer_defect(dim, candidate_properties=dimension_candidate_properties(spec, dim))
        for dim in spec.scoring.dimensions
    )
    return [producer for producer in producers if producer is not None]


def _direction_scoring_defects(
    spec: DomainSpec, direction: str, *, bindable: set[str], reported: set[str], prior: list[str]
) -> list[str]:
    """One direction's dimension defects, then the assembled clause when nothing in it was refused.

    Dimensions compile in declaration order on one assembler, so a later
    dimension sees the parameters earlier ones bound. The clause is assembled
    only when no applicable dimension is ``reported`` and no defect so far
    (``prior`` included) names this direction.
    """
    from engine.scoring.assembler import ScoringAssembler

    assembler = ScoringAssembler(spec)
    applicable = [dim for dim in spec.scoring.dimensions if not dim.matchdirections or direction in dim.matchdirections]
    compiled = (
        _dimension_defect(assembler, dim, direction, bindable) for dim in applicable if dim.name not in reported
    )
    found = [defect for defect in compiled if defect is not None]
    if any(dim.name in reported for dim in applicable):
        return found
    prefix = f"scoring[{direction}]"
    if any(defect.startswith(prefix) for defect in (*prior, *found)):
        return found
    clause = _clause_defect(spec, direction)
    return found if clause is None else [*found, clause]


def _dimension_defect(
    assembler: ScoringAssembler, dim: ScoringDimensionSpec, direction: str, bindable: set[str]
) -> str | None:
    """The compiler's refusal of ``dim``, or the parameters its expression references that nothing binds."""
    try:
        expression = assembler._compile_dimension(dim)
    except (ValueError, KeyError, TypeError) as exc:
        return f"scoring[{direction}] {dim.name}: refused by the scoring compiler — {exc}"
    bound = bindable | set(assembler.last_query_params)
    unbound = sorted({p for p in _PARAM_IDENTIFIER_REF.findall(expression) if p not in bound})
    if not unbound:
        return None
    names = ", ".join(repr(p) for p in unbound)
    return f"scoring[{direction}] {dim.name}: references parameter(s) {names}, which nothing binds"


def _clause_defect(spec: DomainSpec, direction: str) -> str | None:
    """The clause as ``handle_match`` assembles it (aliases, weights, clamp), or ``None`` when it assembles."""
    from engine.scoring.assembler import ScoringAssembler

    try:
        ScoringAssembler(spec).assemble_scoring_clause(direction, {})
    except (ValueError, KeyError, TypeError) as exc:
        return f"scoring[{direction}]: refused by the scoring assembler — {exc}"
    return None


def withholding_reason(spec: DomainSpec, *, complete: bool = False) -> str | None:
    """Why a validated pack is not served, or ``None`` when it is admitted.

    Two generic, fail-closed conditions, each with one explicit operator
    override in ``engine/config/settings.py`` and no knowledge of any
    particular domain:

    1. The pack declares itself dormant (``domain.activation: dormant``). The
       authorized domain owner decides when a pack is ready to serve traffic;
       ``dormant_domain_packs_enabled`` lets an operator serve it anyway.
    2. The pack's gates do not compile to executable Cypher
       (``admission_defects``); ``unvalidated_domain_packs_enabled`` serves it
       anyway, knowing the gates will not execute faithfully.
    """
    if spec.domain.activation is DomainActivation.DORMANT and (complete or not settings.dormant_domain_packs_enabled):
        return (
            f"Domain '{spec.domain.id}' is declared dormant by its pack (domain.activation: dormant); "
            "set DORMANT_DOMAIN_PACKS_ENABLED=true to serve it"
        )
    if complete or not settings.unvalidated_domain_packs_enabled:
        defects = admission_defects(spec, complete=complete)
        if defects:
            # Sync-shape, undeclared-structural-property and scoring-metadata
            # defects are not gate compilation defects, so the reason names every defect.
            from engine.config.outcome_vocabulary import SCORING_METADATA_DEFECT_PREFIX
            from engine.config.structural_vocabulary import STRUCTURAL_DEFECT_PREFIX

            sync_shape = any(
                defect.startswith((*_SYNC_SHAPE_PREFIXES, STRUCTURAL_DEFECT_PREFIX, SCORING_METADATA_DEFECT_PREFIX))
                for defect in defects
            )
            if not complete and not _match_admission_checks_enabled() and not sync_shape:
                return (
                    f"Domain '{spec.domain.id}' is withheld: {len(defects)} gate(s) do not compile to executable "
                    f"Cypher ({defects[0]}); set UNVALIDATED_DOMAIN_PACKS_ENABLED=true to serve it anyway"
                )
            return (
                f"Domain '{spec.domain.id}' is withheld: {len(defects)} admission defect(s) — its gates, traversal, "
                f"scoring or sync declarations do not compile to executable Cypher ({'; '.join(defects)}); "
                "set UNVALIDATED_DOMAIN_PACKS_ENABLED=true to serve it anyway"
            )
    return None


def _match_admission_checks_enabled() -> bool:
    """Whether admission checks more than gates (the pre-remediation wording applies otherwise)."""
    return settings.scoring_admission_check_enabled or settings.admission_checks_full_match


_LEGACY_CACHE_MAX_SIZE_ENV = "DOMAIN_CACHE_MAX_SIZE"


def _domain_cache_max_size() -> int:
    """``settings.domain_cache_maxsize``, honouring the legacy spelling only when the contract one is unset.

    The loader used to read ``DOMAIN_CACHE_MAX_SIZE`` directly, so the
    documented contract variable ``DOMAIN_CACHE_MAXSIZE`` had no effect. An
    operator who set the old name keeps that bound until they rename it.
    """
    legacy = os.getenv(_LEGACY_CACHE_MAX_SIZE_ENV)
    if legacy is not None and "domain_cache_maxsize" not in settings.model_fields_set:
        logger.warning(
            "%s is deprecated; set DOMAIN_CACHE_MAXSIZE instead (honouring %s=%s)",
            _LEGACY_CACHE_MAX_SIZE_ENV,
            _LEGACY_CACHE_MAX_SIZE_ENV,
            legacy,
        )
        return int(legacy)
    return int(settings.domain_cache_maxsize)


def _pack_root_explicitly_configured(raw: str, *, explicit_argument: bool) -> bool:
    """Whether ``raw`` is an operator's choice rather than the Settings default.

    Explicit means DOMAINS_ROOT reached Settings (environment, .env or
    constructor: ``domains_root`` is in ``model_fields_set``), or the caller
    passed a root that differs from the Settings default. Boot passes
    ``str(settings.domains_root)``, so an unset DOMAINS_ROOT is never explicit.
    """
    if "domains_root" in settings.model_fields_set:
        return True
    default = type(settings).model_fields["domains_root"].default
    return explicit_argument and Path(raw) != Path(default)


# Legacy alias for the pack root. Before issue #400 the loader read
# DOMAIN_SPECS_PATH when no root was passed; it keeps that precedence so a
# caller or deployment that sets only it keeps its root. DOMAINS_ROOT
# (Settings.domains_root) is the documented variable.
LEGACY_PACK_ROOT_ENV = "DOMAIN_SPECS_PATH"


def _require_production_pack_root(raw: str, *, explicit_argument: bool, variable: str = "DOMAINS_ROOT") -> None:
    """Issue #400 A4: production never serves packs from an implicit or absent root.

    Under ``L9_ENV=prod`` with ``domains_root_required_in_prod`` on, refuse a
    root that is not explicitly configured, relative, missing, not a directory
    or unreadable. The refusal names DOMAINS_ROOT and is raised from the loader
    boot constructs, so startup fails instead of listing zero domains.
    """
    if not (settings.domains_root_required_in_prod and settings.is_production):
        return
    path = Path(raw)
    reason: str | None = None
    if not _pack_root_explicitly_configured(raw, explicit_argument=explicit_argument):
        reason = "is not set; production has no default pack root"
    elif not path.is_absolute():
        reason = "must be an absolute path in production"
    elif not path.exists():
        reason = "does not exist"
    elif not path.is_dir():
        reason = "is not a directory"
    elif not os.access(path, os.R_OK | os.X_OK):
        reason = "is not readable"
    if reason is not None:
        msg = f"{variable} {reason}: {raw!r} (domains_root_required_in_prod)"
        raise DomainSpecError(msg)


class DomainPackLoader:
    """Load and cache folder-shaped domain specs with bounded hot reload."""

    def __init__(self, config_path: str | None = None) -> None:
        # Issue #400 A5: the root is the caller's argument, then the legacy
        # DOMAIN_SPECS_PATH alias (same precedence as before #400), then
        # Settings.domains_root (env DOMAINS_ROOT). No literal fallback.
        legacy = None if config_path else (os.getenv(LEGACY_PACK_ROOT_ENV) or None)
        raw = config_path or legacy or str(settings.domains_root)
        _require_production_pack_root(
            raw,
            explicit_argument=bool(config_path or legacy),
            variable=LEGACY_PACK_ROOT_ENV if legacy else "DOMAINS_ROOT",
        )
        if settings.domain_pack_receipts_enabled and settings.is_production:
            path = Path(raw)
            if not path.is_absolute() or not path.is_dir() or any(p.is_symlink() for p in [path, *path.parents]):
                raise DomainSpecError("Native production pack root must be explicitly supplied, absolute and present")
        # Do not resolve away symlinks before secure component-by-component opens.
        self._native_root = Path(raw).absolute()
        self._base_path = Path(raw).resolve()
        self._cache: dict[str, tuple[DomainSpec, float, float]] = {}
        # Issue #400 F4: the revision each cached spec was admitted from. Kept
        # in step with ``_cache`` under ``_lock`` (same keys, same bound).
        self._revisions: dict[str, ServedRevision] = {}
        self._lock = threading.Lock()
        # C-24 bounded cache, sized by Settings (contract variables
        # DOMAIN_CACHE_MAXSIZE / DOMAIN_CACHE_TTL_SECONDS, CEG-COR-078).
        self._max_size = _domain_cache_max_size()
        self._ttl_seconds: float = settings.domain_cache_ttl_seconds

    def load_domain(self, domain_id: str) -> DomainSpec:
        """Load a domain only when the generic admission policy admits it.

        Admission is decided by ``withholding_reason`` inside
        ``_load_and_validate``, so a withheld pack is never cached as servable
        and the same verdict applies to ``list_domains``.
        """
        if settings.domain_pack_receipts_enabled:
            return self._load_current(domain_id)[0]
        return self._load_plain(domain_id, require_revision=False)[0]

    def load_domain_revision(self, domain_id: str) -> tuple[DomainSpec, ServedRevision]:
        """Issue #400 F4: the admitted spec together with the revision it came from.

        Same admission as ``load_domain`` (nothing widened); the pair comes from
        one load, so the identity always describes the spec returned beside it.
        """
        if settings.domain_pack_receipts_enabled:
            return self._load_current(domain_id)
        spec, revision = self._load_plain(domain_id, require_revision=True)
        if revision is None:  # pragma: no cover - require_revision guarantees it
            raise DomainSpecError(f"Served revision unavailable for domain {domain_id!r}")
        return spec, revision

    def _load_plain(self, domain_id: str, *, require_revision: bool) -> tuple[DomainSpec, ServedRevision | None]:
        spec_path = self._resolve_spec_path(domain_id)

        with self._lock:
            if domain_id in self._cache:
                cached_spec, cached_mtime, cached_at = self._cache[domain_id]
                cached_revision = self._revisions.get(domain_id)
                usable = cached_revision is not None or not require_revision
                if usable and (time.monotonic() - cached_at) < self._ttl_seconds:
                    return cached_spec, cached_revision
                current_mtime = spec_path.stat().st_mtime
                if usable and cached_mtime >= current_mtime:
                    self._cache[domain_id] = (cached_spec, cached_mtime, time.monotonic())
                    return cached_spec, cached_revision
                logger.info("Domain spec changed on disk, reloading: %s", domain_id)
            else:
                current_mtime = spec_path.stat().st_mtime

            spec, revision = self._load_and_validate(spec_path, domain_id)
            if len(self._cache) >= self._max_size and domain_id not in self._cache:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][2])
                del self._cache[oldest_key]
                self._revisions.pop(oldest_key, None)
                logger.debug("Evicted oldest domain cache entry: %s", oldest_key)

            self._cache[domain_id] = (spec, current_mtime, time.monotonic())
            self._revisions[domain_id] = revision
            return spec, revision

    def invalidate(self, domain_id: str | None = None) -> None:
        """Force cache invalidation."""
        with self._lock:
            if domain_id:
                self._cache.pop(domain_id, None)
                self._revisions.pop(domain_id, None)
            else:
                self._cache.clear()
                self._revisions.clear()

    async def load_domain_async(self, domain_id: str) -> DomainSpec:
        """Async domain loading with per-domain stampede prevention."""
        if settings.domain_pack_receipts_enabled:
            return await asyncio.to_thread(self.load_domain, domain_id)
        with self._lock:
            if domain_id in self._cache:
                cached_spec, _cached_mtime, cached_at = self._cache[domain_id]
                if (time.monotonic() - cached_at) < self._ttl_seconds:
                    return cached_spec

        if not hasattr(self, "_async_locks"):
            self._async_locks: dict[str, asyncio.Lock] = {}
        if domain_id not in self._async_locks:
            self._async_locks[domain_id] = asyncio.Lock()

        async with self._async_locks[domain_id]:
            with self._lock:
                if domain_id in self._cache:
                    cached_spec, _cached_mtime, cached_at = self._cache[domain_id]
                    if (time.monotonic() - cached_at) < self._ttl_seconds:
                        return cached_spec
            return await asyncio.to_thread(self.load_domain, domain_id)

    def list_domains(self) -> list[str]:
        """Discover admitted domain directories containing spec.yaml.

        A directory is listed only when its pack loads under the same
        fail-closed admission policy ``load_domain`` applies, so nothing this
        method names can later be refused as unadmitted. Packs that fail
        validation or are withheld are logged and omitted — never served.
        """
        if not self._base_path.is_dir():
            return []
        admitted: list[str] = []
        for d in sorted(self._base_path.iterdir()):
            filename = "current.json" if settings.domain_pack_receipts_enabled else SPEC_FILENAME
            if not (d.is_dir() and (d / filename).exists()):
                continue
            try:
                self.load_domain(d.name)
            except (DomainNotFoundError, DomainSpecError) as exc:
                logger.info("Domain pack %s not admitted: %s", d.name, exc)
                continue
            admitted.append(d.name)
        return admitted

    def _load_current(self, domain_id: str) -> tuple[DomainSpec, ServedRevision]:
        """Read pointer once, then hash/validate only its fixed immutable revision.

        No legacy fallback and no cache hit after a changed pointer or tamper.
        A concurrent atomic switch yields either complete old or new revision.
        """
        defect = tenant_id_defect(domain_id)
        if defect is not None:
            raise DomainNotFoundError(f"Invalid domain ID: {defect}")
        pointer = read_native_record(self._native_root / domain_id / "current.json", DomainCurrent)
        if pointer.root["tenant_id"] != domain_id:
            raise DomainSpecError("Native current pointer tenant mismatch")
        spec, receipt = load_native_revision(self._native_root, domain_id, pointer.root["sha256"])
        if pointer.root["version"] != spec.domain.version:
            raise DomainSpecError("Native current pointer version mismatch")
        # load_native_revision proved receipt sha256 == sha256 of the admitted bytes.
        return spec, _served_revision(spec, receipt.root["sha256"], "receipt")

    def _resolve_spec_path(self, domain_id: str) -> Path:
        """Resolve and validate spec file path, preventing traversal and symlinks.

        The id is checked against the tenant-id grammar before any path is built
        (CEG-IDENT-01 / CEG-DBNAME-01): tenant id == domain.id == directory ==
        Neo4j database name, so an id the database could not be named by is
        refused here, fail closed, and never reaches the filesystem.
        """
        if not domain_id or not domain_id.strip():
            raise DomainNotFoundError("Domain ID cannot be empty")
        if "\x00" in domain_id:
            raise DomainNotFoundError(f"Invalid domain ID: {domain_id!r} contains null byte")
        if Path(domain_id).is_absolute():
            raise DomainNotFoundError(f"Invalid domain ID: {domain_id!r} must be a relative path")
        defect = tenant_id_defect(domain_id)
        if defect is not None:
            raise DomainNotFoundError(f"Invalid domain ID: {defect}")

        candidate = (self._base_path / domain_id / SPEC_FILENAME).resolve()
        raw_path = self._native_root / domain_id / SPEC_FILENAME
        if raw_path.is_symlink() or raw_path.parent.is_symlink():
            raise DomainNotFoundError(f"Invalid domain path: {domain_id!r} spec.yaml is a symlink")
        try:
            candidate.relative_to(self._base_path.resolve())
        except ValueError as exc:
            raise DomainNotFoundError(f"Invalid domain path: {domain_id!r} resolves outside base directory") from exc
        if not candidate.exists():
            raise DomainNotFoundError(f"Domain spec not found: {candidate}")
        # Legacy lookup preserves authorized root/ancestor symlinks by resolving.
        # Receipt admission instead opens _native_root with strict O_NOFOLLOW.
        return candidate

    def _load_and_validate(self, path: Path, domain_id: str) -> tuple[DomainSpec, ServedRevision]:
        """Delegate schema and actual admission to the shared native owner.

        Reads the file once; the digest is of exactly the bytes admitted
        (the same read and the same ``validate_native_bytes`` call that
        ``validate_native_file`` makes for this caller).
        """
        data = read_native_file(path)
        spec = validate_native_bytes(data, domain_id)
        return spec, _served_revision(spec, hashlib.sha256(data).hexdigest(), "plain")

    def list_domain_revisions(self) -> list[ServedRevision]:
        """Issue #400 F4: ``list_domains`` with each admitted tenant's served revision.

        One admission pass; a pack ``list_domains`` would omit is omitted here.
        """
        if not self._base_path.is_dir():
            return []
        filename = "current.json" if settings.domain_pack_receipts_enabled else SPEC_FILENAME
        admitted: list[ServedRevision] = []
        for d in sorted(self._base_path.iterdir()):
            if not (d.is_dir() and (d / filename).exists()):
                continue
            try:
                _spec, revision = self.load_domain_revision(d.name)
            except (DomainNotFoundError, DomainSpecError) as exc:
                logger.info("Domain pack %s not admitted: %s", d.name, exc)
                continue
            admitted.append(revision)
        return admitted
