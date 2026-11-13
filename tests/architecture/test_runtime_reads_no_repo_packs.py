"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, architecture, domain-loader]
owner: engine-team
status: active
--- /L9_META ---

tests/architecture/test_runtime_reads_no_repo_packs.py
Issue #400 A6 (engine half): runtime code names no repository pack directory.

Production serves only packs an operator publishes into the mounted pack root
(DOMAINS_ROOT). So no Python file under engine/ or chassis/ may name
tests/fixtures (design_packs or domains), and none may carry a bare ``domains``
path literal used as a pack root, except the one Settings default
(``engine/config/settings.py``: ``domains_root: Path = Path("./domains")``).

The scan is over string constants in the AST. A ``domains`` literal used as a
mapping key or subscript (the ``list_domains`` response key) is not a path. The
negative witnesses prove each shape the guard exists to catch is caught. The
Docker half is tests/architecture/test_runtime_images_ship_no_repo_packs.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIRS = ("engine", "chassis")
SETTINGS_FILE = "engine/config/settings.py"

_REPO_PACK_DIR = re.compile(r"tests[/\\]fixtures|design_packs")
# "domains", "./domains", "domains/", "domains/<x>/spec.yaml", "./domains/<x>"
_BARE_DOMAINS_PATH = re.compile(r"^(\./)?domains(/.*)?$")


def _is_settings_default(node: ast.AST, rel: str) -> bool:
    """The single allowed pack-root literal: Settings.domains_root's default."""
    return (
        rel == SETTINGS_FILE
        and isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "domains_root"
    )


def _key_constants(tree: ast.AST) -> set[int]:
    """Constants used as dict keys or subscripts: names, not paths."""
    keys: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys.update(id(k) for k in node.keys if isinstance(k, ast.Constant))
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            keys.add(id(node.slice))
    return keys


def violations(source: str, rel: str) -> list[str]:
    """Every repository-pack reference in ``source`` (file ``rel``)."""
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if _is_settings_default(node, rel):
            allowed.update(id(sub) for sub in ast.walk(node))
    keys = _key_constants(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if _REPO_PACK_DIR.search(node.value):
            found.append(f"{rel}:{node.lineno}: names a repository pack directory: {node.value!r}")
        elif _BARE_DOMAINS_PATH.match(node.value.strip()) and id(node) not in allowed | keys:
            found.append(f"{rel}:{node.lineno}: bare 'domains' pack-root literal: {node.value!r}")
    return found


def _runtime_files() -> list[Path]:
    return sorted(p for d in RUNTIME_DIRS for p in (ROOT / d).rglob("*.py"))


def test_runtime_tree_is_scanned() -> None:
    files = _runtime_files()
    assert any(p.relative_to(ROOT).as_posix() == SETTINGS_FILE for p in files)
    assert any(p.relative_to(ROOT).as_posix() == "engine/config/loader.py" for p in files)
    assert any(p.parts[-2] == "chassis" for p in files)


def test_settings_default_is_the_only_pack_root_literal() -> None:
    source = (ROOT / SETTINGS_FILE).read_text(encoding="utf-8")
    assert 'domains_root: Path = Path("./domains")' in source
    assert violations(source, SETTINGS_FILE) == []


def test_runtime_reads_no_repository_packs() -> None:
    found: list[str] = []
    for path in _runtime_files():
        rel = path.relative_to(ROOT).as_posix()
        found.extend(violations(path.read_text(encoding="utf-8"), rel))
    assert found == [], "\n".join(found)


# ── Negative witnesses: each shape must be caught ────────


@pytest.mark.parametrize(
    "source",
    [
        'ROOT = "tests/fixtures/design_packs"\n',
        'ROOT = "tests/fixtures/domains"\n',
        'from pathlib import Path\nROOT = Path(__file__).parent / "design_packs"\n',
        'from pathlib import Path\nROOT = Path("domains")\n',
        'from pathlib import Path\nROOT = Path("./domains/")\n',
        'import os\nraw = os.getenv("X") or "domains"\n',
        'import os\nraw = os.getenv("DOMAIN_SPECS_PATH", "domains")\n',
        'def load(domains_dir: str = "domains") -> None: ...\n',
        'SPEC = "domains/plasticos/spec.yaml"\n',
    ],
)
def test_guard_catches_pack_directory_literals(source: str) -> None:
    assert violations(source, "engine/example.py")


def test_guard_catches_settings_shape_outside_settings_file() -> None:
    """The Settings exemption is bound to engine/config/settings.py only."""
    source = 'from pathlib import Path\nclass S:\n    domains_root: Path = Path("./domains")\n'
    assert violations(source, "chassis/auth/settings.py")
    assert violations(source, SETTINGS_FILE) == []


def test_guard_catches_second_literal_in_settings_file() -> None:
    source = (
        "from pathlib import Path\n"
        "class S:\n"
        '    domains_root: Path = Path("./domains")\n'
        '    legacy_root: Path = Path("domains")\n'
    )
    assert len(violations(source, SETTINGS_FILE)) == 1


@pytest.mark.parametrize(
    "source",
    [
        'def f(x: list[str]) -> dict[str, list[str]]:\n    return {"domains": x}\n',
        'def f(p: dict[str, int]) -> int:\n    return p["domains"]\n',
        'NOTE = "list_domains returns admitted domains"\n',
    ],
)
def test_guard_accepts_non_path_uses(source: str) -> None:
    assert violations(source, "engine/example.py") == []
