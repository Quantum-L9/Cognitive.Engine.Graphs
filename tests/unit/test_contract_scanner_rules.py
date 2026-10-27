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


def test_main_exits_one_on_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "x.py").write_text("import fastapi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cs.sys, "argv", ["contract_scanner.py"])
    assert cs.main() == 1
