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
import shutil
import subprocess
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


# ── verification mode (RU-131, RG-166) ──────────────────────────────────────
#
# main() is the C-18 gate (contracts.yml meta-headers, pre-commit
# l9-meta-check). Each case builds a throwaway git repository and swaps the
# module's registry and baseline for a two-file one, so the verdict depends
# only on the scratch tree.

ENGINE_HEADER_FM = inj.FileMeta("engine/core.py", "engine-specific", ["engine"], ["core"], "engine-team")
DOC_FM = inj.FileMeta("docs/guide.md", "engine-specific", ["docs"], ["guide"], "engine-team")


GIT = shutil.which("git")


def _git(root: Path, *args: str) -> None:
    assert GIT is not None, "git is required for the verification-mode tests"
    subprocess.run([GIT, *args], cwd=root, check=True, capture_output=True)  # noqa: S603


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo whose only eligible files carry the header the registry expects."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "engine/core.py", inj.inject_meta('"""Core."""\nX = 1\n', ENGINE_HEADER_FM, "graph"))
    _write(root, "docs/guide.md", inj.inject_meta("# Guide\n", DOC_FM, "graph"))
    _write(root, "engine/__init__.py", "")
    _write(root, "notes.txt", "out of the contract's scope\n")
    _git(root, "add", "--", "engine/core.py", "docs/guide.md", "engine/__init__.py", "notes.txt")
    monkeypatch.setattr(inj, "FILE_REGISTRY", [ENGINE_HEADER_FM, DOC_FM])
    monkeypatch.setattr(inj, "HEADERLESS_BASELINE", frozenset())
    return root


def _verdict(argv: list[str]) -> int | None:
    try:
        return inj.main(argv)
    except SystemExit as exc:  # argparse usage error
        return 2 if exc.code is None else int(exc.code)


@pytest.mark.parametrize("mode", [[], ["check"]])
def test_fully_headered_tree_exits_0(scratch_repo: Path, mode: list[str]) -> None:
    assert _verdict([*mode, "--root", str(scratch_repo)]) == 0


@pytest.mark.parametrize("mode", [[], ["check"]])
def test_removed_header_exits_1(scratch_repo: Path, mode: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    _write(scratch_repo, "engine/core.py", '"""Core."""\nX = 1\n')
    assert _verdict([*mode, "--root", str(scratch_repo)]) == 1
    assert "engine/core.py" in capsys.readouterr().out


@pytest.mark.parametrize("mode", [[], ["check"]])
def test_new_tracked_engine_file_without_header_is_reported(
    scratch_repo: Path, mode: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _write(scratch_repo, "engine/gates/new_gate.py", '"""New gate."""\n')
    _git(scratch_repo, "add", "--", "engine/gates/new_gate.py")
    assert _verdict([*mode, "--root", str(scratch_repo)]) == 1
    out = capsys.readouterr().out
    assert "engine/gates/new_gate.py" in out
    assert "NOHEADER" in out


def test_untracked_file_is_not_eligible(scratch_repo: Path) -> None:
    _write(scratch_repo, "engine/scratch.py", "X = 1\n")
    assert _verdict(["check", "--root", str(scratch_repo)]) == 0


def test_unregistered_file_with_canonical_header_passes(scratch_repo: Path) -> None:
    fm = inj.FileMeta("engine/extra.py", "engine-specific", ["engine"], ["extra"], "engine-team")
    _write(scratch_repo, "engine/extra.py", inj.inject_meta("X = 1\n", fm, "graph"))
    _git(scratch_repo, "add", "--", "engine/extra.py")
    assert _verdict(["check", "--root", str(scratch_repo)]) == 0


def test_unregistered_schema_v2_header_is_invalid(scratch_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    v2 = (
        '"""\n--- L9_META ---\nl9_schema: 2\norigin: chassis\nengine: graph\n'
        'layer: [api]\ntags: [chassis]\nstatus: active\n--- /L9_META ---\n"""\n'
    )
    _write(scratch_repo, "chassis/app.py", v2)
    _git(scratch_repo, "add", "--", "chassis/app.py")
    assert _verdict(["check", "--root", str(scratch_repo)]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_registered_file_absent_from_tree_exits_1(scratch_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = inj.FileMeta("engine/gone.py", "engine-specific", ["engine"], ["gone"], "engine-team")
    monkeypatch.setattr(inj, "FILE_REGISTRY", [ENGINE_HEADER_FM, DOC_FM, gone])
    assert _verdict(["check", "--root", str(scratch_repo)]) == 1


def test_headerless_baseline_entry_is_exempt(scratch_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(scratch_repo, "docs/legacy.md", "# Legacy\n")
    _git(scratch_repo, "add", "--", "docs/legacy.md")
    assert _verdict(["check", "--root", str(scratch_repo)]) == 1
    monkeypatch.setattr(inj, "HEADERLESS_BASELINE", frozenset({"docs/legacy.md"}))
    assert _verdict(["check", "--root", str(scratch_repo)]) == 0


def test_check_never_writes(scratch_repo: Path) -> None:
    _write(scratch_repo, "engine/core.py", '"""Core."""\nX = 1\n')
    assert _verdict(["check", "--root", str(scratch_repo)]) == 1
    assert (scratch_repo / "engine" / "core.py").read_text(encoding="utf-8") == '"""Core."""\nX = 1\n'


def test_apply_repairs_then_check_passes(scratch_repo: Path) -> None:
    _write(scratch_repo, "engine/core.py", '"""Core."""\nX = 1\n')
    assert _verdict(["--apply", "--root", str(scratch_repo)]) == 0
    assert _verdict(["check", "--root", str(scratch_repo)]) == 0


def test_check_and_apply_together_are_rejected(scratch_repo: Path) -> None:
    assert _verdict(["check", "--apply", "--root", str(scratch_repo)]) == 2


def test_root_outside_a_git_repository_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inj, "FILE_REGISTRY", [])
    monkeypatch.setattr(inj, "HEADERLESS_BASELINE", frozenset())
    assert _verdict(["check", "--root", str(tmp_path)]) == 1


# ── discovery configuration stays in lockstep with the contract ─────────────


def test_scope_patterns_match_contract_18() -> None:
    import yaml

    contract = yaml.safe_load((REPO / "contracts" / "contract_18.yaml").read_text(encoding="utf-8"))
    assert list(inj.SCOPE_PATTERNS) == contract["scope"]["paths"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("engine/gates/x.py", True),
        ("setup.py", True),
        ("Makefile", True),
        ("sub/Makefile", True),
        ("chassis/Dockerfile.chassis", True),
        ("notes.txt", False),
        (".env.template", False),
    ],
)
def test_in_scope(path: str, expected: bool) -> None:
    assert inj.in_scope(path) is expected


def test_registry_has_no_duplicate_or_excluded_paths() -> None:
    paths = [fm.path for fm in inj.FILE_REGISTRY]
    assert len(paths) == len(set(paths))
    assert not [p for p in paths if inj.is_excluded(p)]
    assert not set(paths) & inj.HEADERLESS_BASELINE


def test_generated_artifacts_are_excluded() -> None:
    for rel in ("artifacts/audit_report.md", "artifacts/coverage_matrix.json", "artifacts/coverage_report.md"):
        assert inj.is_excluded(rel)


def test_retired_domain_extractor_is_not_registered_at_any_path() -> None:
    """RU-054 (DEC-19 A) retired tools/domain_extractor.py; no registry entry may name it."""
    paths = {fm.path for fm in inj.FILE_REGISTRY}
    assert "tools/domain_extractor.py" not in paths
    assert "scripts/domain_extractor.py" not in paths


# ── markdown: only the header is stripped, never an example in a code fence ──


def test_markdown_example_in_code_fence_survives() -> None:
    example = "```markdown\n<!-- L9_META\nl9_schema: 1\n/L9_META -->\n```\n"
    once = inj.inject_meta("# Doc\n\n" + example, _meta("docs/x.md"), "graph")
    assert once.endswith(example)
    assert inj.inject_meta(once, _meta("docs/x.md"), "graph") == once
