"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, capability, auth]
owner: engine-team
status: active
--- /L9_META ---

AUTH-DELEGATE-04 (docs/contracts/auth/rbac-policy.yaml): root registration and
caller identity bound to canonical ingress.

``CapabilityValidator.register`` is the only way a root grant enters the
validator registry; ``derive_capability`` only narrows an existing parent, and
``delegate_capability`` refuses when no owned parent is registered. So the
binding holds as long as no production path mints a root. This module proves
that statically, over every production module (``engine/`` and ``chassis/``):

- nothing calls ``register`` on a capability validator;
- nothing constructs a ``Capability`` except ``engine/auth/capabilities.py``;
- nothing writes into a validator's ``_registry`` except that module.

A production root-grant path would therefore be a deliberate change that fails
here, not a drive-by. The detector is exercised against synthetic positives so
a silent scanner cannot pass the suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_PRODUCTION_PACKAGES = ("engine", "chassis")
_OWNER = Path("engine/auth/capabilities.py")
_VALIDATOR_SOURCES = frozenset({"get_capability_validator", "CapabilityValidator"})
_REGISTRY_MUTATORS = frozenset({"update", "setdefault", "__setitem__"})


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _validator_names(tree: ast.AST) -> set[str]:
    """Names bound, anywhere in the module, to a capability validator."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            if _call_name(node.value) in _VALIDATOR_SOURCES:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _is_validator(node: ast.expr, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    return _call_name(node) in _VALIDATOR_SOURCES


def _is_registry(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "_registry"


def root_minting_sites(source: str, path: str) -> list[str]:
    """Every place in ``source`` that could put a root grant in the registry."""
    tree = ast.parse(source, filename=path)
    names = _validator_names(tree)
    sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "register" and _is_validator(func.value, names):
                sites.append(f"{path}:{node.lineno}: validator.register(...)")
            if _call_name(node) == "Capability":
                sites.append(f"{path}:{node.lineno}: Capability(...) constructed")
            if isinstance(func, ast.Attribute) and func.attr in _REGISTRY_MUTATORS and _is_registry(func.value):
                sites.append(f"{path}:{node.lineno}: _registry.{func.attr}(...)")
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and _is_registry(node.value):
            sites.append(f"{path}:{node.lineno}: _registry[...] assigned")
    return sites


def _production_modules() -> list[Path]:
    modules = [p for pkg in _PRODUCTION_PACKAGES for p in sorted((_REPO / pkg).rglob("*.py"))]
    return [p for p in modules if p.relative_to(_REPO) != _OWNER]


class TestNoProductionRootRegistration:
    def test_production_tree_is_scanned(self) -> None:
        modules = {p.relative_to(_REPO).as_posix() for p in _production_modules()}
        assert "engine/handlers.py" in modules
        assert "chassis/handler_registration.py" in modules
        assert _OWNER.as_posix() not in modules

    def test_no_production_module_can_mint_a_root_grant(self) -> None:
        sites = [
            site
            for module in _production_modules()
            for site in root_minting_sites(module.read_text(encoding="utf-8"), module.relative_to(_REPO).as_posix())
        ]
        assert sites == []

    def test_handlers_still_reach_the_validator_only_through_narrowing_calls(self) -> None:
        source = (_REPO / "engine/handlers.py").read_text(encoding="utf-8")
        assert "get_capability_validator()" in source
        assert "derive_capability(" in source
        assert root_minting_sites(source, "engine/handlers.py") == []


class TestDetectorCatchesEveryMintingForm:
    @pytest.mark.parametrize(
        ("snippet", "expected"),
        [
            ("v = get_capability_validator()\nv.register(cap)\n", "validator.register"),
            ("get_capability_validator().register(cap)\n", "validator.register"),
            ("v: CapabilityValidator = CapabilityValidator()\nv.register(cap)\n", "validator.register"),
            ("cap = Capability(tenant_id='t', domain_id='*', allowed_actions=frozenset())\n", "Capability(...)"),
            ("caps.Capability(tenant_id='t', domain_id='*', allowed_actions=frozenset())\n", "Capability(...)"),
            ("v = get_capability_validator()\nv._registry['x'] = cap\n", "_registry[...] assigned"),
            ("get_capability_validator()._registry.update({'x': cap})\n", "_registry.update"),
            ("v._registry.setdefault('x', cap)\n", "_registry.setdefault"),
        ],
    )
    def test_minting_form_is_reported(self, snippet: str, expected: str) -> None:
        sites = root_minting_sites(snippet, "synthetic.py")
        assert len(sites) == 1
        assert expected in sites[0]

    @pytest.mark.parametrize(
        "snippet",
        [
            "health.register('neo4j', check)\n",
            "v = get_capability_validator()\nfor cap in v._registry.values():\n    pass\n",
            "v = get_capability_validator()\nv.derive_capability(parent, {}, target_tenant='t')\n",
            "v = get_capability_validator()\nv.revoke_capability('x')\n",
        ],
    )
    def test_reads_and_narrowing_are_not_reported(self, snippet: str) -> None:
        assert root_minting_sites(snippet, "synthetic.py") == []
