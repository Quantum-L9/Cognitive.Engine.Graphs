"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [contract-scanner, audit]
owner: platform
status: active
--- /L9_META ---

Unit tests for the AST-evaluated contract_scanner rules (RU-137, RG-173).

SEC-007, ARCH-001/002 and DEL-001/002 used to be line regexes: they flagged
``yaml.load(f, Loader=yaml.SafeLoader)`` and missed multi-line unsafe loads,
submodule / bare imports of fastapi and starlette, aliased httpx modules and
calls on client objects. They are now evaluated on the module AST with the
same Violation output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import contract_scanner as cs

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

YAML_CASES = """import yaml
a = yaml.load(open("x.yaml"))
b = yaml.load(f)  # unsafe, trailing comment
c = yaml.load(f, Loader=yaml.SafeLoader)
d = yaml.load(
    f
)
e = yaml.safe_load(f)
g = yaml.load(f, Loader=yaml.CSafeLoader)
h = yaml.load(f, yaml.SafeLoader)
i = yaml.load(f, Loader=yaml.FullLoader)
"""

ARCH_CASES = """from fastapi.routing import APIRouter
from starlette.requests import Request
import fastapi
import httpx as hx
import httpx


async def call(url):
    async with httpx.AsyncClient() as client:
        await client.post(url)
    hx.post(url)
"""


def _hits(source: str, path: str = "engine/x.py") -> list[tuple[str, int]]:
    return sorted((v.rule_id, v.line) for v in cs.scan_file(Path(path), source, REPO))


def _rule_lines(source: str, rule_id: str, path: str = "engine/x.py") -> list[int]:
    return [line for rid, line in _hits(source, path) if rid == rule_id]


# ── SEC-007 ─────────────────────────────────────────────────────────────────


def test_sec007_flags_unsafe_loads_including_multiline() -> None:
    assert _rule_lines(YAML_CASES, "SEC-007") == [2, 3, 5, 11]


def test_sec007_never_flags_safe_loader_or_safe_load() -> None:
    lines = _rule_lines(YAML_CASES, "SEC-007")
    for safe_line in (4, 8, 9, 10):
        assert safe_line not in lines


def test_sec007_resolves_module_alias_and_from_import() -> None:
    src = "import yaml as y\nfrom yaml import load\nx = y.load(f)\nz = load(f)\nw = load(f, Loader=y.SafeLoader)\n"
    assert _rule_lines(src, "SEC-007") == [3, 4]


def test_sec007_ignores_yaml_text_in_strings_and_comments() -> None:
    src = 'MSG = "never call yaml.load(f)"\n# yaml.load(f) is banned\n'
    assert _rule_lines(src, "SEC-007") == []


# ── ARCH-001 / ARCH-002 ─────────────────────────────────────────────────────


def test_arch_rules_flag_submodule_and_bare_imports() -> None:
    assert _rule_lines(ARCH_CASES, "ARCH-001") == [1, 3]
    assert _rule_lines(ARCH_CASES, "ARCH-002") == [2]


@pytest.mark.parametrize(
    ("src", "rule"),
    [
        ("from fastapi import FastAPI\n", "ARCH-001"),
        ("import fastapi.routing\n", "ARCH-001"),
        ("import os, fastapi as fa\n", "ARCH-001"),
        ("from starlette import status\n", "ARCH-002"),
        ("import starlette.middleware.cors\n", "ARCH-002"),
    ],
)
def test_arch_rules_import_forms(src: str, rule: str) -> None:
    assert _rule_lines(src, rule) == [1]


def test_arch_rules_ignore_lookalike_modules_and_relative_imports() -> None:
    src = "import fastapi_utils\nfrom .fastapi import x\nfrom mystarlette import y\n"
    assert _rule_lines(src, "ARCH-001") == []
    assert _rule_lines(src, "ARCH-002") == []


def test_arch_rules_scoped_to_engine() -> None:
    assert _rule_lines(ARCH_CASES, "ARCH-001", path="chassis/app.py") == []
    assert _rule_lines(ARCH_CASES, "ARCH-002", path="chassis/app.py") == []


# ── DEL-001 / DEL-002 ───────────────────────────────────────────────────────


def test_del001_flags_alias_and_client_object_calls() -> None:
    assert _rule_lines(ARCH_CASES, "DEL-001") == [10, 11]


@pytest.mark.parametrize(
    "src",
    [
        "import httpx as hx\nhx.post(u)\n",
        "import httpx\nhttpx.AsyncClient().post(u)\n",
        "from httpx import post\npost(u)\n",
        "from httpx import Client\nc = Client()\nc.get(u)\n",
        "import httpx\nclass A:\n    def f(self):\n        self.c = httpx.Client()\n        self.c.put(u)\n",
    ],
)
def test_del001_forms(src: str) -> None:
    assert _rule_lines(src, "DEL-001") == [len(src.splitlines())]


@pytest.mark.parametrize(
    "src",
    [
        "import requests as rq\nrq.get(u)\n",
        "import requests\nrequests.Session().post(u)\n",
        "import requests\nwith requests.Session() as s:\n    s.delete(u)\n",
        "from requests import patch\npatch(u)\n",
    ],
)
def test_del002_forms(src: str) -> None:
    assert _rule_lines(src, "DEL-002") == [len(src.splitlines())]


def test_del_rules_ignore_unrelated_get_calls() -> None:
    src = "d = {}\nd.get('k')\nclient = object()\nclient.post(1)\n"
    assert _rule_lines(src, "DEL-001") == []
    assert _rule_lines(src, "DEL-002") == []


@pytest.mark.parametrize(
    "src",
    [
        # a parameter of another function that shares the client's name
        "import httpx\ndef make():\n    client = httpx.Client()\n    return client\ndef read(client):\n    return client.get('k')\n",
        # rebound to a non-HTTP object before the call
        "import httpx\ndef f():\n    client = httpx.Client()\n    client.close()\n    client = {}\n    return client.get('k')\n",
        # a same-named local in a sibling function
        "import httpx\ndef a():\n    client = httpx.Client()\n    return client\ndef b():\n    client = {'k': 1}\n    return client.get('k')\n",
        # a loop target that shadows a module-level client
        "import httpx\nclient = httpx.Client()\ndef f(caches):\n    for client in caches:\n        client.get('k')\n",
        # a class attribute is not visible to its methods as a bare name
        "import httpx\nclass A:\n    client = httpx.Client()\n    def f(self, client):\n        return client.get('k')\n",
    ],
)
def test_del001_client_names_are_scoped_to_their_bindings(src: str) -> None:
    """RU-137 review: a client binding certifies only the uses it can reach."""
    assert _rule_lines(src, "DEL-001") == []


def test_del001_flags_nonlocal_client_binding() -> None:
    src = (
        "import httpx\n"
        "def outer():\n"
        "    client = None\n"
        "    def init():\n"
        "        nonlocal client\n"
        "        client = httpx.Client()\n"
        "    def call(u):\n"
        "        return client.get(u)\n"
    )
    assert _rule_lines(src, "DEL-001") == [8]


@pytest.mark.parametrize(
    "src",
    [
        # module-level client used inside a function
        "import httpx\nclient = httpx.Client()\ndef f(u):\n    return client.get(u)\n",
        # module singleton assigned through `global`
        "import httpx\nclient = None\ndef init():\n    global client\n    client = httpx.Client()\ndef f(u):\n    return client.get(u)\n",
        # closure over an enclosing function's client
        "import httpx\ndef outer(u):\n    client = httpx.Client()\n    def inner():\n        return client.get(u)\n    return inner\n",
        # a conditional client binding still reaches the call
        "import httpx\ndef f(flag, u):\n    client = {}\n    if flag:\n        client = httpx.Client()\n    return client.get(u)\n",
        # an annotated binding, and a later use in the same block
        "import httpx\ndef f(u):\n    client: httpx.Client = httpx.Client()\n    x = 1\n    return client.get(u)\n",
        # bound in a loop body and called on a later iteration
        "import httpx\ndef f(us):\n    for u in us:\n        if u:\n            client.get(u)\n        client = httpx.Client()\n",
    ],
)
def test_del001_scoped_client_names_still_flag(src: str) -> None:
    lines = src.splitlines()
    expected = [i for i, line in enumerate(lines, start=1) if "client.get(" in line]
    assert _rule_lines(src, "DEL-001") == expected


def test_del001_flags_attribute_client_bound_by_with() -> None:
    src = (
        "import httpx\n"
        "class Api:\n"
        "    def call(self, u):\n"
        "        with httpx.Client() as self.client:\n"
        "            return self.client.post(u)\n"
    )
    assert _rule_lines(src, "DEL-001") == [5]


def test_del_rules_scoped_to_engine() -> None:
    assert _rule_lines(ARCH_CASES, "DEL-001", path="tools/x.py") == []


# ── shared invariants ───────────────────────────────────────────────────────


def test_rule_ids_and_severities_unchanged() -> None:
    by_id = {r["id"]: r for r in cs.RULES}
    for rid in ("SEC-007", "ARCH-001", "ARCH-002", "DEL-001", "DEL-002"):
        assert by_id[rid]["severity"] == "CRITICAL"
    assert by_id["ARCH-001"]["include_dirs"] == [cs.ENGINE_DIR]
    assert by_id["DEL-002"]["include_dirs"] == [cs.ENGINE_DIR]


def test_unparsable_file_falls_back_to_line_regex() -> None:
    src = "def broken(:\n    yaml.load(f)\nfrom fastapi import X\n"
    hits = _hits(src)
    assert ("SEC-007", 2) in hits
    assert ("ARCH-001", 3) in hits


def test_scan_file_parses_once_for_all_ast_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    parse_calls = 0
    parse = cs.ast.parse

    def count_parse(*args: object, **kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return parse(*args, **kwargs)

    monkeypatch.setattr(cs.ast, "parse", count_parse)
    src = "import yaml\nfrom fastapi import FastAPI\nimport httpx\nyaml.load(f)\nhttpx.get(u)\n"

    cs.scan_file(Path("engine/x.py"), src, REPO)

    assert parse_calls == 1


def test_unparsable_file_respects_ast_rule_path_scope() -> None:
    src = "def broken(:\nfrom fastapi import FastAPI\nimport httpx\nhttpx.get(u)\n"
    assert _hits(src, path="tools/broken.py") == []


def test_main_exits_one_on_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "x.py").write_text("import fastapi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cs.sys, "argv", ["contract_scanner.py"])
    assert cs.main() == 1


def test_main_rejects_explicit_escape_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    escaped = tmp_path / "outside.py"
    escaped.write_text("eval('outside')\n", encoding="utf-8")
    scanned: list[Path] = []
    monkeypatch.chdir(root)
    monkeypatch.setattr(cs.sys, "argv", ["contract_scanner.py", str(escaped)])
    monkeypatch.setattr(cs, "scan_file", lambda path, text, scan_root: scanned.append(path) or [])

    assert cs.main() == 2
    assert scanned == []
    assert "pre-commit Python file must stay under --root" in capsys.readouterr().err


def test_main_skips_venv_during_default_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ignored = tmp_path / ".venv" / "lib" / "ignored.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("eval('ignored')\n", encoding="utf-8")
    scanned: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cs.sys, "argv", ["contract_scanner.py"])
    monkeypatch.setattr(cs, "scan_file", lambda path, text, scan_root: scanned.append(path) or [])

    assert cs.main() == 0
    assert scanned == []


# ── ERR-002 (RU-136, RG-172; DEC-51: enforced in engine/ only) ──────────────

ERR002_FIXTURE = """def swallow():
    try:
        risky()
    except ValueError:
        pass
"""

ERR002_CASES = """def a():
    try:
        x()
    except (TypeError, ValueError):
        pass


def b():
    try:
        x()
    except KeyError:
        pass
        pass


def c():
    try:
        x()
    except ValueError as exc:
        log_and_raise(exc)


def d():
    try:
        x()
    except ValueError:
        # documented, but still nothing but pass
        pass
    finally:
        pass


def e():
    try:
        x()
    {BARE_EXCEPT}
        pass


def f():
    try:
        x()
    except OSError: pass
""".replace("{BARE_EXCEPT}", "except" + ":")  # spelled apart so ERR-001 does not flag this test file


def test_err002_fixture_produces_one_violation_at_the_except_line() -> None:
    assert _rule_lines(ERR002_FIXTURE, "ERR-002", path="engine/err002.py") == [4]


def test_err002_flags_only_pass_only_typed_handlers() -> None:
    # a(): tuple type, b(): several passes, d(): comment + pass, f(): one-line form.
    # c() handles the error; e() is a bare except (ERR-001's finding, not ERR-002's).
    assert _rule_lines(ERR002_CASES, "ERR-002", path="engine/cases.py") == [4, 11, 26, 43]


def test_err002_scoped_to_engine() -> None:
    assert _rule_lines(ERR002_FIXTURE, "ERR-002", path="tools/err002.py") == []
    assert _rule_lines(ERR002_FIXTURE, "ERR-002", path="tests/test_err002.py") == []


def test_err002_unparsable_file_falls_back_to_one_line_form() -> None:
    src = "def broken(:\n    try:\n        x()\n    except OSError: pass\n"
    assert _rule_lines(src, "ERR-002", path="engine/broken.py") == [4]


def test_err002_exemption_is_keyed_on_path_function_and_type() -> None:
    src = (
        "class GraphLifecycle:\n"
        "    async def shutdown(self):\n"
        "        try:\n"
        "            await task\n"
        "        except asyncio.CancelledError:\n"
        "            pass\n"
        "        try:\n"
        "            await other\n"
        "        except ValueError:\n"
        "            pass\n"
    )
    # The CancelledError handler in GraphLifecycle.shutdown is exempt; ValueError is not.
    assert _rule_lines(src, "ERR-002", path="engine/boot.py") == [9]
    # The same code in another module has no exemption.
    assert _rule_lines(src, "ERR-002", path="engine/other.py") == [5, 9]


def test_err002_every_exemption_has_a_reason_and_a_live_handler() -> None:
    import ast

    live = set()
    for rel, qualname, exc in cs.ERR_002_EXEMPTIONS:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        live |= {(rel, q, e) for q, e, _ in cs.swallowed_handlers(tree) if (rel, q, e) == (rel, qualname, exc)}
    assert live == set(cs.ERR_002_EXEMPTIONS)
    assert all(reason.strip() for reason in cs.ERR_002_EXEMPTIONS.values())


def test_err002_engine_tree_is_clean() -> None:
    hits = []
    for path in sorted((REPO / "engine").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        hits += [(v.file, v.line) for v in cs.scan_file(path, source, REPO) if v.rule_id == "ERR-002"]
    assert hits == []
