#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [audit]
tags: [delivery, harness]
status: active
--- /L9_META ---

L9 Contract Violation Scanner
Encodes the grep-able subset of the 24 contracts as regex rules; rules whose meaning
depends on call arguments or import structure (SEC-007, ARCH-001/002, DEL-001/002)
are evaluated on the module AST instead. Contracts without a
mechanical signature are verified by tests/contracts/, not here — see contracts/*.yaml
`verification.scanner_rules` for the authoritative rule-to-contract mapping.
Exit code 1 = violations found = commit/merge blocked.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CYPHER_SAFETY_MD = "CYPHER_SAFETY.md"
BANNED_PATTERNS_MD = "BANNED_PATTERNS.md"
SHARED_MODELS_MD = "SHARED_MODELS.md"
ENGINE_DIR = "engine/"
CONTRACT_SCANNER_PATH = "tools/contract_scanner.py"


@dataclass
class Violation:
    file: str
    line: int
    rule_id: str
    contract: str
    severity: str
    message: str
    remediation: str


def _rule(
    rule_id: str,
    contract: str,
    severity: str,
    pattern: str,
    message: str,
    *,
    remediation: str,
    include_dirs: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    ast_check: AstCheck | None = None,
) -> dict:
    r = {
        "id": rule_id,
        "contract": contract,
        "severity": severity,
        "pattern": pattern,
        "message": message,
        "remediation": remediation,
    }
    if include_dirs is not None:
        r["include_dirs"] = include_dirs
    if exclude_dirs is not None:
        r["exclude_dirs"] = exclude_dirs
    if ast_check is not None:
        r["ast_check"] = ast_check
    return r


# =============================================================================
# AST-evaluated rules
# =============================================================================
# An AST check receives the parsed module and returns the 1-based line numbers
# of its violations. Rules carrying one keep their regex ``pattern`` only as the
# fallback for a file that does not parse.

AstCheck = Callable[[ast.Module], list[int]]

_HTTP_METHODS = frozenset({"post", "get", "put", "delete", "patch"})
_SAFE_YAML_LOADERS = frozenset({"SafeLoader", "CSafeLoader"})


def _module_aliases(tree: ast.Module, module: str) -> tuple[set[str], dict[str, str]]:
    """Names bound to *module* itself, and ``{local_name: member}`` for ``from module import member``."""
    modules: set[str] = set()
    members: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module:
                    modules.add(alias.asname or module)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module:
            for alias in node.names:
                members[alias.asname or alias.name] = alias.name
    return modules, members


def _is_member_ref(node: ast.expr, modules: set[str], members: dict[str, str], wanted: frozenset[str]) -> bool:
    """``mod.<wanted>`` via a module alias, or a bare name imported from the module as one of *wanted*."""
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id in modules and node.attr in wanted
    if isinstance(node, ast.Name):
        return members.get(node.id) in wanted
    return False


def _check_yaml_load(tree: ast.Module) -> list[int]:
    """SEC-007: yaml.load whose Loader is missing or not SafeLoader / CSafeLoader."""
    modules, members = _module_aliases(tree, "yaml")
    lines: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_member_ref(node.func, modules, members, frozenset({"load"}))):
            continue
        loader = next((kw.value for kw in node.keywords if kw.arg == "Loader"), None)
        if loader is None and len(node.args) >= 2:  # yaml.load(stream, Loader)
            loader = node.args[1]
        if loader is None or not _is_member_ref(loader, modules, members, _SAFE_YAML_LOADERS):
            lines.append(node.lineno)
    return lines


def _import_check(top_level: str) -> AstCheck:
    """Any absolute import whose top-level module is *top_level*."""

    def check(tree: ast.Module) -> list[int]:
        lines: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == top_level for alias in node.names):
                    lines.append(node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] == top_level:
                    lines.append(node.lineno)
        return lines

    return check


def _http_call_check(module: str, client_factories: frozenset[str]) -> AstCheck:
    """Raw HTTP via *module*: module-level calls, imported functions, and calls on client objects."""

    def check(tree: ast.Module) -> list[int]:
        modules, members = _module_aliases(tree, module)
        if not modules and not members:
            return []

        def is_client_factory_call(node: ast.expr) -> bool:
            return isinstance(node, ast.Call) and _is_member_ref(node.func, modules, members, client_factories)

        # Expressions bound to a client object: ``c = httpx.Client()``,
        # ``self.c = httpx.Client()``, ``with httpx.AsyncClient() as c``.
        clients: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                if is_client_factory_call(node.value):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    clients.update(ast.unparse(t) for t in targets)
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if item.optional_vars is not None and is_client_factory_call(item.context_expr):
                        clients.add(ast.unparse(item.optional_vars))

        lines: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if _is_member_ref(func, modules, members, _HTTP_METHODS):
                lines.append(node.lineno)
            elif isinstance(func, ast.Attribute) and func.attr in _HTTP_METHODS:
                receiver = func.value
                if is_client_factory_call(receiver) or ast.unparse(receiver) in clients:
                    lines.append(node.lineno)
        return lines

    return check


RULES: list[dict] = [
    # -- CONTRACT 3: CYPHER_SAFETY.md --
    _rule(
        "SEC-001",
        CYPHER_SAFETY_MD,
        "CRITICAL",
        r'f["\'].*MATCH\s*\(.*\{[^$]',
        "Cypher label interpolation without sanitize_label()",
        remediation="Use sanitize_label() for labels, $param for values",
        include_dirs=[ENGINE_DIR],
        exclude_dirs=[
            "engine/sync/generator.py",
            "engine/handlers.py",
            "engine/causal/causal_compiler.py",
            "engine/causal/attribution.py",
        ],  # labels sanitized via sanitize_label()
    ),
    _rule(
        "SEC-002",
        CYPHER_SAFETY_MD,
        "CRITICAL",
        r"\beval\s*\(",
        "eval() is banned - code injection risk",
        remediation="Use operator dispatch table or ast.literal_eval()",
        exclude_dirs=["tests/", CONTRACT_SCANNER_PATH, "engine/utils/safe_eval.py"],  # AST-based; no eval()
    ),
    _rule(
        "SEC-003",
        CYPHER_SAFETY_MD,
        "CRITICAL",
        r"\bexec\s*\(",
        "exec() is banned - code injection risk",
        remediation="Remove entirely",
        exclude_dirs=["tests/", CONTRACT_SCANNER_PATH, "engine/security/"],
    ),
    _rule(
        "SEC-004",
        CYPHER_SAFETY_MD,
        "CRITICAL",
        r'f["\'].*LIMIT\s*\{',
        "LIMIT value interpolation - use $limit parameter",
        remediation="LIMIT $limit with params={'limit': n}",
    ),
    _rule(
        "SEC-005",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r'f["\'].*(?:SELECT|INSERT|UPDATE|DELETE)\s.*\{',
        "SQL string interpolation - use parameterized queries",
        remediation="Use $1/$2 placeholders or ORM",
    ),
    _rule(
        "SEC-006",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r"pickle\.loads?\s*\(",
        "pickle banned - deserialization attack vector",
        remediation="Use json.loads()",
    ),
    _rule(
        "SEC-007",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r"yaml\.load\s*\([^)]*\)\s*$",
        "yaml.load() without SafeLoader",
        remediation="yaml.safe_load()",
        ast_check=_check_yaml_load,
    ),
    # -- CONTRACT 4: ERROR_HANDLING.md --
    _rule(
        "ERR-001",
        "ERROR_HANDLING.md",
        "HIGH",
        r"except\s*:",
        "Bare except: clause",
        remediation="except SpecificError as e:",
        exclude_dirs=[CONTRACT_SCANNER_PATH],
    ),
    _rule(
        "ERR-002",
        "ERROR_HANDLING.md",
        "HIGH",
        r"except\s+\w+.*:\s*\n\s*pass",
        "Swallowed exception - except + pass",
        remediation="Log and re-raise",
    ),
    # -- CONTRACT 10: BANNED_PATTERNS.md (Architecture) --
    _rule(
        "ARCH-001",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r"from\s+fastapi\s+import",
        "FastAPI import in engine/ - chassis owns HTTP",
        remediation="Register handlers in engine/handlers.py",
        include_dirs=[ENGINE_DIR],
        ast_check=_import_check("fastapi"),
    ),
    _rule(
        "ARCH-002",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r"from\s+starlette\s+import",
        "Starlette import in engine/ - chassis owns middleware",
        remediation="Remove",
        include_dirs=[ENGINE_DIR],
        ast_check=_import_check("starlette"),
    ),
    _rule(
        "ARCH-003",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r"import\s+uvicorn",
        "uvicorn import in engine/ - chassis owns ASGI",
        remediation="Remove",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 7: DEPENDENCY_INJECTION.md --
    _rule(
        "DI-001",
        "DEPENDENCY_INJECTION.md",
        "HIGH",
        r"from\s+fastapi\s+import\s+Depends",
        "FastAPI Depends in engine/ - chassis concern",
        remediation="Use init_dependencies() pattern",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 12: DELEGATION_PROTOCOL.md --
    _rule(
        "DEL-001",
        "DELEGATION_PROTOCOL.md",
        "CRITICAL",
        r"httpx\.(post|get|put|delete|patch)\s*\(",
        "Raw HTTP to another node - use delegate_to_node()",
        remediation="from engine.packet.chassis_contract import delegate_to_node",
        include_dirs=[ENGINE_DIR],
        ast_check=_http_call_check("httpx", frozenset({"Client", "AsyncClient"})),
    ),
    _rule(
        "DEL-002",
        "DELEGATION_PROTOCOL.md",
        "CRITICAL",
        r"requests\.(post|get|put|delete|patch)\s*\(",
        "Raw HTTP via requests - use delegate_to_node()",
        remediation="from engine.packet.chassis_contract import delegate_to_node",
        include_dirs=[ENGINE_DIR],
        ast_check=_http_call_check("requests", frozenset({"Session", "session"})),
    ),
    # -- CONTRACT 19: MEMORY_SUBSTRATE_ACCESS.md --
    _rule(
        "MEM-001",
        "MEMORY_SUBSTRATE_ACCESS.md",
        "CRITICAL",
        r"INSERT\s+INTO\s+packetstore",
        "Direct write to packetstore - use ingest_packet()",
        remediation="Persist via engine.packet.packet_store, or delegate to the memory substrate node",
        include_dirs=[ENGINE_DIR],
    ),
    _rule(
        "MEM-002",
        "MEMORY_SUBSTRATE_ACCESS.md",
        "CRITICAL",
        r"INSERT\s+INTO\s+memory_embeddings",
        "Direct write to memory_embeddings - use ingest_packet()",
        remediation="Embeddings generated by LangGraph DAG",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 20: SHARED_MODELS.md --
    _rule(
        "SHARED-001",
        SHARED_MODELS_MD,
        "HIGH",
        r"class\s+TransportPacket\s*\(",
        "Redefining TransportPacket - import the shared model",
        remediation="from constellation_node_sdk import TransportPacket",
        include_dirs=[ENGINE_DIR],
        exclude_dirs=["engine/packet/packet_envelope.py"],  # canonical envelope in this repo
    ),
    _rule(
        "SHARED-002",
        SHARED_MODELS_MD,
        "HIGH",
        r"class\s+TenantContext\s*\(",
        "Redefining TenantContext - import the shared model",
        remediation="from engine.packet.packet_envelope import TenantContext",
        include_dirs=[ENGINE_DIR],
        exclude_dirs=["engine/packet/packet_envelope.py"],  # canonical envelope in this repo
    ),
    _rule(
        "SHARED-003",
        SHARED_MODELS_MD,
        "HIGH",
        r"class\s+ExecuteRequest\s*\(",
        "Redefining ExecuteRequest - the chassis owns this model",
        remediation="from chassis.chassis_app import ExecuteRequest",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 18: OBSERVABILITY.md --
    _rule(
        "OBS-001",
        "OBSERVABILITY.md",
        "HIGH",
        r"structlog\.configure\s*\(",
        "Configuring structlog in engine - chassis does this",
        remediation="Use logging.getLogger(__name__)",
        include_dirs=[ENGINE_DIR],
    ),
    _rule(
        "OBS-002",
        "OBSERVABILITY.md",
        "HIGH",
        r"logging\.basicConfig\s*\(",
        "Configuring logging in engine - chassis does this",
        remediation="Use logging.getLogger(__name__) only",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 6: PYDANTIC_YAML_MAPPING.md --
    _rule(
        "NAME-001",
        "PYDANTIC_YAML_MAPPING.md",
        "HIGH",
        r"Field\s*\(\s*alias\s*=",
        "Pydantic Field alias banned - snake_case everywhere",
        remediation="Remove alias, use snake_case matching YAML key",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 13: PACKET_TYPE_REGISTRY.md --
    _rule(
        "PKT-001",
        "PACKET_TYPE_REGISTRY.md",
        "HIGH",
        r'packet_type\s*[=:]\s*["\'][A-Z]',
        "Uppercase packet_type - must be lowercase snake_case",
        remediation="Check PACKET_TYPE_REGISTRY.md",
        exclude_dirs=["agents/cursor/"],
    ),
    # -- CONTRACT 17: zero-stub protocol (TEST_PATTERNS.md / BANNED_PATTERNS.md) --
    # Scoped to engine/: abstract-base-class methods in chassis/ legitimately raise
    # NotImplementedError as their contract.
    _rule(
        "STUB-001",
        BANNED_PATTERNS_MD,
        "CRITICAL",
        r"raise\s+NotImplementedError",
        "Stub in engine/ - unimplemented code path",
        remediation="Ship the implementation or record the gap in DEFERRED.md",
        include_dirs=[ENGINE_DIR],
    ),
    _rule(
        "STUB-002",
        BANNED_PATTERNS_MD,
        "HIGH",
        r"#\s*TODO\b",
        "TODO comment in engine/ - deferred work must be tracked, not inlined",
        remediation="Implement it now or add an entry to DEFERRED.md and drop the comment",
        include_dirs=[ENGINE_DIR],
    ),
    _rule(
        "STUB-003",
        BANNED_PATTERNS_MD,
        "HIGH",
        r"#\s*(?:PLACEHOLDER|FIXME|XXX)\b",
        "PLACEHOLDER/FIXME comment in engine/ - deferred work must be tracked, not inlined",
        remediation="Implement it now or add an entry to DEFERRED.md and drop the comment",
        include_dirs=[ENGINE_DIR],
    ),
    # -- CONTRACT 05: ENV_VARS.md --
    _rule(
        "ENV-001",
        "ENV_VARS.md",
        "MEDIUM",
        r'os\.environ\[["\']?(?:NEO4J_URI|NEO4J_URL|DATABASE_URL|REDIS_HOST|API_KEY)["\']?\]',
        "Non-standard env var name",
        remediation="Use L9_* or ENGINE_* prefix per ENV_VARS.md",
    ),
]

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}


def _path_matches_rule(file_path: Path, rule: dict) -> bool:
    path_str = str(file_path) + "/"
    if include_dirs := rule.get("include_dirs"):
        if not any(path_str.startswith(d) for d in include_dirs):
            return False
    if exclude_dirs := rule.get("exclude_dirs"):
        if any(path_str.startswith(d) for d in exclude_dirs):
            return False
    return True


def scan_file(file_path: Path, content: str, root: Path) -> list[Violation]:
    violations: list[Violation] = []
    # Handle both absolute and relative paths
    try:
        abs_path = file_path.resolve()
        abs_root = root.resolve()
        rel = abs_path.relative_to(abs_root)
    except ValueError:
        # Path is already relative or not under root
        rel = file_path
    rel_str = str(rel).replace("\\", "/")

    tree: ast.Module | None = None
    parsed = False

    for rule in RULES:
        if not _path_matches_rule(Path(rel_str), rule):
            continue
        ast_check = rule.get("ast_check")
        if ast_check is not None and not parsed:
            parsed = True
            try:
                tree = ast.parse(content)
            except (SyntaxError, ValueError):
                tree = None  # unparsable: AST rules fall back to their line regex
        if ast_check is not None and tree is not None:
            hit_lines = sorted(set(ast_check(tree)))
        else:
            try:
                pat = re.compile(rule["pattern"], re.MULTILINE | re.DOTALL)
            except re.error:
                continue
            hit_lines = [i for i, line in enumerate(content.splitlines(), start=1) if pat.search(line)]
        violations.extend(
            Violation(
                file=rel_str,
                line=i,
                rule_id=rule["id"],
                contract=rule["contract"],
                severity=rule["severity"],
                message=rule["message"],
                remediation=rule["remediation"],
            )
            for i in hit_lines
        )
    return violations


def main() -> int:
    root = Path.cwd()
    skip_dirs = {".venv", "venv", "__pycache__", ".git", "site-packages"}
    if len(sys.argv) > 1:
        # Pre-commit passes filenames
        paths = [Path(p) for p in sys.argv[1:] if Path(p).suffix == ".py" and not any(s in str(p) for s in skip_dirs)]
    else:
        paths = [p for p in root.rglob("*.py") if not any(s in str(p) for s in skip_dirs)]

    all_violations: list[Violation] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        all_violations.extend(scan_file(path, text, root))

    all_violations.sort(key=lambda v: (SEVERITY_ORDER.get(v.severity, 99), v.file, v.line))

    if not all_violations:
        print("L9 contract scan: no violations.")
        return 0

    print("L9 Contract Violations (commit/merge blocked):\n", file=sys.stderr)
    for v in all_violations:
        print(
            f"  [{v.rule_id}] {v.file}:{v.line} ({v.severity})\n    {v.message}\n    → {v.remediation}\n",
            file=sys.stderr,
        )
    print(
        f"Total: {len(all_violations)} violation(s). Fix before committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
