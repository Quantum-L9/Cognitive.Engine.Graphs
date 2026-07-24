"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [test]
tags: [platform]
status: active
--- /L9_META ---

Golden fixtures for the L9_META format layer (Phase 0 gate).

Every format must satisfy two properties:

- round-trip: ``render -> parse -> render`` is stable
- idempotency: ``inject(inject(x)) == inject(x)``

The regression cases below encode the three hazards found in the working tree:
legacy bare-``# L9_META`` headers that the old strip pattern missed, YAML front
matter that must stay at byte 0, and paths containing spaces or uppercase
extensions.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.l9_meta.formats import (
    comment,
    detect_filetype,
    docstring,
    for_path,
    html,
    jsonmeta,
    tomlmeta,
)
from tools.l9_meta.model import SCHEMA_VERSION, MetaRecord

pytestmark = pytest.mark.unit


REC = MetaRecord(
    origin="l9-template",
    engine="graph",
    layer=("meta",),
    tags=("meta", "injector"),
    status="active",
)

TEXT_FORMATS = [comment, docstring, html]
ALL_FORMATS = [comment, docstring, html, jsonmeta, tomlmeta]


# --- Fixture bodies -----------------------------------------------------------

LEGACY_PY = '''# L9_META
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [security]
# tags: [security, llm, re-export]
# owner: platform
# status: active
# /L9_META
"""Re-export LLM security utilities."""

from engine.security._5_llm_security import sanitize
'''

LEGACY_YAML_BANNER = """# ============================================================================
# L9_META
# ============================================================================
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [agent-rules, kernel]
# tags: [cursor, kernel, governance]
# owner: Founder
# status: active
# ============================================================================

kernel_version: 1.7
"""

FRONT_MATTER_MD = """---
name: contract-check
description: Verify the 24 contracts
paths:
  - "engine/**"
---

# Contract Check

Body text.
"""

PROSE_MD = """# Docs

Every tracked file carries an L9_META header. The comment form looks like:

```yaml
# --- L9_META ---
# l9_schema: 2
# --- /L9_META ---
```

That is prose, not a header.
"""


# --- Round-trip and idempotency ----------------------------------------------


@pytest.mark.parametrize("fmt", ALL_FORMATS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_render_parse_render_is_stable(fmt) -> None:
    seed = "{}\n" if fmt is jsonmeta else ""
    injected = fmt.inject(seed, REC)
    parsed = fmt.parse(injected)
    assert parsed is not None, "header must be parseable after injection"
    assert parsed.normalized() == REC.normalized()
    assert fmt.inject(seed, parsed) == injected


@pytest.mark.parametrize(
    ("fmt", "body"),
    [
        (comment, "key: value\n"),
        (comment, "#!/usr/bin/env bash\nset -e\n"),
        (docstring, '"""Module docs."""\n\nX = 1\n'),
        (docstring, "X = 1\n"),
        (docstring, LEGACY_PY),
        (html, "# Title\n\nBody.\n"),
        (html, FRONT_MATTER_MD),
        (jsonmeta, '{\n  "a": 1\n}\n'),
        (tomlmeta, "[tool.ruff]\nline-length = 120\n"),
    ],
    ids=[
        "comment-plain",
        "comment-shebang",
        "docstring-existing",
        "docstring-none",
        "docstring-legacy",
        "html-plain",
        "html-front-matter",
        "json-object",
        "toml-table",
    ],
)
def test_inject_is_idempotent(fmt, body: str) -> None:
    once = fmt.inject(body, REC)
    assert fmt.inject(once, REC) == once


@pytest.mark.parametrize("fmt", TEXT_FORMATS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_strip_removes_every_header(fmt) -> None:
    injected = fmt.inject("original content\n", REC)
    assert "L9_META" not in fmt.strip(injected)


# --- Legacy bare-form regressions --------------------------------------------


def test_legacy_python_header_is_replaced_not_duplicated() -> None:
    result = docstring.inject(LEGACY_PY, REC)
    assert result.count("L9_META") == 2, "exactly one block (open + close sentinel)"
    assert "# L9_META" not in result
    assert result.startswith('"""')
    assert "Re-export LLM security utilities." in result
    assert "from engine.security._5_llm_security import sanitize" in result


def test_legacy_banner_yaml_header_is_replaced_not_duplicated() -> None:
    result = comment.inject(LEGACY_YAML_BANNER, REC)
    assert result.count("L9_META") == 2
    assert result.startswith(comment.OPEN)
    assert "kernel_version: 1.7" in result
    assert "# ====" not in result.split(comment.CLOSE)[0]


def test_legacy_header_is_parseable() -> None:
    parsed = docstring.parse(LEGACY_PY)
    assert parsed is not None
    assert parsed.schema == 1
    assert parsed.layer == ("security",)


# --- Self-documentation must not be read as a header --------------------------

INDENTED_EXAMPLE_PY = '''"""Explains the format.

Canonical form:

    --- L9_META ---
    l9_schema: 2
    --- /L9_META ---
"""

import re
'''

INDENTED_EXAMPLE_YAML = """# Explains the format:
#
#     # --- L9_META ---
#     # l9_schema: 2
#     # --- /L9_META ---
key: value
"""

FIXTURE_DEEP_IN_FILE = '''"""Real docstring, no header."""

SAMPLE = """
# --- L9_META ---
# l9_schema: 2
# --- /L9_META ---
"""
'''


@pytest.mark.parametrize(
    ("fmt", "source"),
    [(docstring, INDENTED_EXAMPLE_PY), (comment, INDENTED_EXAMPLE_YAML)],
)
def test_indented_example_is_not_a_header(fmt, source: str) -> None:
    """An indented delimiter documents the format; it is not a header.

    The block is always emitted flush-left, so indentation is the signal. Both
    formatter modules carry such an example in their own docstring and were
    reporting themselves as malformed.
    """
    assert fmt.parse(source) is None
    assert fmt.strip(source) == source, "documentation must survive untouched"


def test_delimiter_inside_a_string_literal_is_not_a_header() -> None:
    """A header lives in the file's leading comment region, not anywhere in it.

    An unbounded scan reads a fixture string 100 lines down as the header, then
    reports it malformed because the surrounding code has no field lines.
    """
    assert docstring.parse(FIXTURE_DEEP_IN_FILE) is None


def test_this_test_module_is_not_self_detected() -> None:
    """The fixtures above are real L9_META text at column 0 in this very file."""
    source = Path(__file__).read_text(encoding="utf-8")
    parsed = docstring.parse(source)
    assert parsed is None or parsed.schema == SCHEMA_VERSION


# --- Python statement-order and formatting regressions ------------------------

FUTURE_ONLY = "from __future__ import annotations\n\nimport re\n"
COMMENT_PREAMBLE = "# ruff: noqa: E501\n\nfrom __future__ import annotations\n"


@pytest.mark.parametrize("source", [FUTURE_ONLY, COMMENT_PREAMBLE])
def test_future_import_stays_first_statement(source: str) -> None:
    """`from __future__` must remain the first statement or Python refuses the file.

    Injecting a second string literal above an existing docstring demotes it to a
    plain expression, which pushes `__future__` out of the prologue.
    """
    result = docstring.inject(source, REC)
    ast.parse(result)
    assert result.count('"""') == 2, "one docstring, not two literals"


def test_new_docstring_is_followed_by_a_blank_line() -> None:
    """ruff format requires a blank line after a module docstring.

    Without it `ruff format --check .` reformats the file, so a full stamp would
    fail CI on every module that had no docstring to merge into.
    """
    result = docstring.inject(FUTURE_ONLY, REC)
    assert '"""\n\nfrom __future__' in result


def test_shebang_survives_injection() -> None:
    result = docstring.inject("#!/usr/bin/env python3\nimport os\n", REC)
    assert result.startswith("#!/usr/bin/env python3\n")
    ast.parse(result)


# --- Front-matter protection --------------------------------------------------


def test_front_matter_stays_on_line_one() -> None:
    result = html.inject(FRONT_MATTER_MD, REC)
    assert result.splitlines()[0] == "---"
    assert result.splitlines()[1] == "name: contract-check"


def test_header_lands_after_front_matter() -> None:
    result = html.inject(FRONT_MATTER_MD, REC)
    front_end = result.index("---\n", result.index("paths:"))
    assert result.index(html.OPEN) > front_end


def test_front_matter_survives_yaml_parse() -> None:
    yaml = pytest.importorskip("yaml")
    result = html.inject(FRONT_MATTER_MD, REC)
    front_matter, _ = html.split_front_matter(result)
    parsed = yaml.safe_load(front_matter.strip().strip("-"))
    assert parsed["name"] == "contract-check"


def test_markdown_without_front_matter_gets_header_first() -> None:
    result = html.inject("# Title\n", REC)
    assert result.startswith(html.OPEN)


# --- Prose must never be mistaken for a header --------------------------------


def test_prose_mention_is_not_parsed_as_header() -> None:
    assert html.parse(PROSE_MD) is None


def test_prose_fenced_comment_form_is_untouched_by_html_strip() -> None:
    assert html.strip(PROSE_MD) == PROSE_MD


def test_contract_reference_line_is_not_a_legacy_header() -> None:
    text = "# CONTRACT 18: L9_META ON EVERY FILE\n# tags: [a]\nkey: value\n"
    assert comment.parse(text) is None
    assert comment.strip(text) == text


# --- Path handling ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/ACTION ITEMS.MD", "html"),
        ("docs/PlasticOS Graph Cognitive Engine.yaml", "comment"),
        ("engine/packet/README-Packet Envelope.md", "html"),
        ("Makefile", "comment"),
        ("Dockerfile.prod", "comment"),
        ("CODEOWNERS", "comment"),
        (".cursorrules", "comment"),
        ("templates/.env.vps.template", "comment"),
        ("engine/handlers.py", "docstring"),
        ("pyproject.toml", "toml"),
        ("artifacts/coverage_matrix.json", "json"),
        (".cursor/rules/00-global.mdc", "html"),
        ("uv.lock", None),
        ("docs/diagram.png", None),
    ],
)
def test_detect_filetype(path: str, expected: str | None) -> None:
    assert detect_filetype(path) == expected


def test_path_with_space_dispatches_to_a_format() -> None:
    fmt = for_path("docs/ACTION ITEMS.MD")
    assert fmt is html
    result = fmt.inject("# Action Items\n", REC)
    assert result.startswith(html.OPEN)


# --- Schema compatibility -----------------------------------------------------


def test_writer_emits_v2_and_drops_owner() -> None:
    rendered = comment.render(REC)
    assert f"l9_schema: {SCHEMA_VERSION}" in rendered
    assert "owner" not in rendered


def test_parser_accepts_v1_and_normalizes_to_v2() -> None:
    v1 = comment.inject("key: value\n", REC).replace(
        f"# l9_schema: {SCHEMA_VERSION}",
        "# l9_schema: 1\n# owner: platform",
    )
    parsed = comment.parse(v1)
    assert parsed is not None
    assert parsed.schema == 1
    assert parsed.normalized().schema == SCHEMA_VERSION


def test_json_meta_key_is_first() -> None:
    result = jsonmeta.inject('{\n  "a": 1\n}\n', REC)
    assert next(iter(json.loads(result))) == jsonmeta.KEY


def test_json_array_is_not_injectable() -> None:
    with pytest.raises(jsonmeta.NotInjectableError):
        jsonmeta.inject("[1, 2]\n", REC)


def test_unknown_field_is_rejected() -> None:
    from tools.l9_meta.model import MetaError

    broken = comment.inject("key: value\n", REC).replace("# status: active", "# bogus: x\n# status: active")
    with pytest.raises(MetaError):
        comment.parse(broken)
