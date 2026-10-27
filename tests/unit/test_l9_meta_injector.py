"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [meta, injector, contract-18]
owner: platform
status: active
--- /L9_META ---

Unit tests for tools/l9_meta_injector.py (RU-130, RG-165).

The injector is a pure ``inject_meta(content, FileMeta, engine) -> str``
transform. These tests pin the four properties ``--apply`` depends on:
every formatter emits the complete delimited field block, a second
injection is byte-identical to the first, Python handling never touches
anything but the module docstring, and the injector applied to its own
source changes nothing but its header.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tools import l9_meta_injector as inj

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

FIELD_ORDER = ["l9_schema", "origin", "engine", "layer", "tags", "owner", "status"]


def _meta(path: str) -> inj.FileMeta:
    return inj.FileMeta(path, "engine-specific", ["config"], ["alpha", "beta"], "engine-team")


def _expected_fields(prefix: str) -> list[str]:
    return [
        f"{prefix}--- L9_META ---",
        f"{prefix}l9_schema: {inj.L9_SCHEMA_VERSION}",
        f"{prefix}origin: engine-specific",
        f"{prefix}engine: graph",
        f"{prefix}layer: [config]",
        f"{prefix}tags: [alpha, beta]",
        f"{prefix}owner: engine-team",
        f"{prefix}status: active",
        f"{prefix}--- /L9_META ---",
    ]


def _header_lines(text: str, prefix: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(f"{prefix}--- L9_META ---")
    end = lines.index(f"{prefix}--- /L9_META ---")
    return lines[start : end + 1]


# ── full field block ────────────────────────────────────────────────────────

COMMENT_CASES = [
    ("scripts/run.sh", "#!/usr/bin/env bash\nset -euo pipefail\necho hi\n"),
    ("domains/x/spec.yaml", "domain:\n  id: x\n"),
    ("Makefile", "all:\n\techo hi\n"),
    ("Dockerfile", "FROM python:3.12-slim\nRUN true\n"),
    (".github/CODEOWNERS", "* @owner\n"),
]


@pytest.mark.parametrize(("path", "content"), COMMENT_CASES)
def test_comment_style_header_has_all_seven_fields(path: str, content: str) -> None:
    out = inj.inject_meta(content, _meta(path), "graph")
    assert _header_lines(out, "# ") == _expected_fields("# ")


def test_format_comment_block_field_order_matches_contract() -> None:
    block = inj.format_comment_block(_meta("a.sh"), "graph")
    keys = [ln[2:].split(":")[0] for ln in block.splitlines()[1:-1]]
    assert keys == FIELD_ORDER


def test_format_python_docstring_block_has_all_fields() -> None:
    block = inj.format_python_docstring_block(_meta("a.py"), "graph")
    assert block.splitlines() == _expected_fields("")


def test_python_header_has_all_seven_fields() -> None:
    out = inj.inject_meta('"""Module."""\nimport os\n', _meta("pkg/x.py"), "graph")
    assert _header_lines(out, "") == _expected_fields("")
    assert ast.get_docstring(ast.parse(out)) is not None
    assert "--- L9_META ---" in (ast.get_docstring(ast.parse(out)) or "")


def test_shebang_stays_on_line_one() -> None:
    out = inj.inject_meta("#!/usr/bin/env bash\necho hi\n", _meta("a.sh"), "graph")
    assert out.startswith("#!/usr/bin/env bash\n# --- L9_META ---\n")


# ── idempotence ─────────────────────────────────────────────────────────────

IDEMPOTENCE_CASES = [
    *COMMENT_CASES,
    ("pkg/mod.py", '"""Module doc."""\n\nimport os\n'),
    ("pkg/nodoc.py", "import os\n\nX = 1\n"),
    ("pkg/shebang.py", '#!/usr/bin/env python3\n"""Doc.\n\nMore.\n"""\nimport os\n'),
    ("pkg/empty.py", ""),
    ("docs/x.md", "# Title\n\nBody\n"),
    ("cfg/x.json", '{"a": 1}\n'),
    ("pyproject.toml", '[project]\nname = "x"\n'),
]


@pytest.mark.parametrize(("path", "content"), IDEMPOTENCE_CASES)
def test_second_injection_is_byte_identical(path: str, content: str) -> None:
    meta = _meta(path)
    once = inj.inject_meta(content, meta, "graph")
    twice = inj.inject_meta(once, meta, "graph")
    assert twice == once


@pytest.mark.parametrize(("path", "content"), COMMENT_CASES)
def test_comment_style_injection_keeps_body(path: str, content: str) -> None:
    out = inj.inject_meta(content, _meta(path), "graph")
    for line in content.splitlines():
        assert line in out.splitlines()


# ── Python: confined to the module docstring ────────────────────────────────

CASE_A = '''"""
--- L9_META ---
l9_schema: 1
--- /L9_META ---

Module.
"""

def fmt(prefix):
    lines = [
        f"{prefix}--- L9_META ---",
        f"{prefix}l9_schema: 1",
        f"{prefix}--- /L9_META ---",
    ]
    return lines
'''

CASE_B = '''# -*- coding: utf-8 -*-
# Copyright 2026 Example. Licensed under Apache-2.0.
"""Module doc."""
import os
'''

CASE_C = '''import textwrap

TEMPLATE = (
"""
hello
"""
)
'''


def test_case_a_markers_in_code_string_literals_survive() -> None:
    out = inj.inject_meta(CASE_A, _meta("pkg/x.py"), "graph")
    for line in (
        '        f"{prefix}--- L9_META ---",',
        '        f"{prefix}l9_schema: 1",',
        '        f"{prefix}--- /L9_META ---",',
    ):
        assert line in out.splitlines()
    ast.parse(out)
    assert "Module." in (ast.get_docstring(ast.parse(out)) or "")


def test_case_b_coding_and_licence_comments_survive() -> None:
    out = inj.inject_meta(CASE_B, _meta("pkg/x.py"), "graph")
    assert out.startswith("# -*- coding: utf-8 -*-\n# Copyright 2026 Example. Licensed under Apache-2.0.\n")
    assert "import os" in out.splitlines()
    doc = ast.get_docstring(ast.parse(out)) or ""
    assert "--- L9_META ---" in doc
    assert "Module doc." in doc


def test_case_c_no_docstring_keeps_code() -> None:
    out = inj.inject_meta(CASE_C, _meta("pkg/x.py"), "graph")
    tree = ast.parse(out)
    assert "--- L9_META ---" in (ast.get_docstring(tree) or "")
    assert "import textwrap" in out.splitlines()
    assert "TEMPLATE = (" in out.splitlines()
    assert out.endswith(CASE_C)
    # The original literal is still an assignment, not a hijacked docstring.
    assert isinstance(tree.body[1], ast.Import)
    assert isinstance(tree.body[2], ast.Assign)


def test_no_docstring_inserted_after_comment_prologue() -> None:
    src = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n# licence\nimport os\n"
    out = inj.inject_meta(src, _meta("pkg/x.py"), "graph")
    assert out.startswith('#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n# licence\n"""\n--- L9_META ---\n')
    assert out.endswith("import os\n")
    ast.parse(out)


def test_python_code_after_docstring_untouched() -> None:
    code = 'X = "--- L9_META ---"\n# --- L9_META ---\n# --- /L9_META ---\nY = "--- /L9_META ---"\n'
    src = '"""Doc."""\n' + code
    out = inj.inject_meta(src, _meta("pkg/x.py"), "graph")
    assert out.endswith(code)


def test_python_without_module_docstring_but_comment_meta_in_code() -> None:
    src = "import os\n\n# --- L9_META ---\n# note\n# --- /L9_META ---\nX = 1\n"
    out = inj.inject_meta(src, _meta("pkg/x.py"), "graph")
    assert out.endswith(src)


# ── self-application safety ─────────────────────────────────────────────────


def _function_sources(text: str) -> dict[str, str]:
    tree = ast.parse(text)
    return {
        node.name: ast.get_source_segment(text, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_self_application_changes_only_the_header() -> None:
    path = REPO / "tools" / "l9_meta_injector.py"
    source = path.read_text(encoding="utf-8")
    fm = next(f for f in inj.FILE_REGISTRY if f.path == "tools/l9_meta_injector.py")
    out = inj.inject_meta(source, fm, "graph")
    assert _function_sources(out) == _function_sources(source)
    # Everything after the module docstring is byte-identical.
    tail = re.split(r'^"""$', source, maxsplit=2, flags=re.MULTILINE)[2]
    assert out.endswith(tail)
    assert inj.inject_meta(out, fm, "graph") == out
