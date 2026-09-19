"""Unit tests — tools/cypher_lint.py C-009 scanner."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.cypher_lint import scan_tree


@pytest.mark.unit
def test_quoted_interpolation_fails(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    target = engine / "sync"
    target.mkdir()
    (target / "generator.py").write_text(
        "cypher = f\"SET n.status = '{status}'\"\n",
        encoding="utf-8",
    )

    findings = scan_tree(tmp_path)

    assert findings, "quoted value interpolation must fail the scanner"
    assert findings[0].rel_path == "engine/sync/generator.py"
    assert "status" in findings[0].pattern


@pytest.mark.unit
def test_sanitize_label_interpolation_passes(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "ok.py").write_text(
        'from engine.utils.security import sanitize_label\ncypher = f"MATCH (n:{sanitize_label(label)}) RETURN n"\n',
        encoding="utf-8",
    )

    findings = scan_tree(tmp_path)

    assert findings == []
