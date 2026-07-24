"""
Research-pattern coverage wiring contract.

``tools/research/top5_leverage_patterns_detailed.json`` is an input to
``tools/spec_extract.py``, not reference reading. Each pattern declares an
``engine_mapping`` naming the real engine symbols that implement it, and the
extractor turns those into rows in ``artifacts/coverage_matrix.json``.

The mapping is the fragile part: engine code gets renamed or moved, and a stale
token silently reports a live capability as MISSING (or a dead one as covered).
These tests keep the mapping honest — a rename that breaks it fails CI instead
of quietly corrupting the gap report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.spec_extract import (  # noqa: E402
    RESEARCH_DIR,
    RESEARCH_PATTERNS_FILE,
    extract_research_features,
)

RESEARCH_FILE = REPO_ROOT / RESEARCH_DIR / RESEARCH_PATTERNS_FILE


def _patterns() -> dict[str, dict]:
    data = json.loads(RESEARCH_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


PATTERNS = _patterns() if RESEARCH_FILE.is_file() else {}
PATTERN_IDS = list(PATTERNS)


def test_research_file_exists() -> None:
    """spec_extract reads this path directly; a move must update the constant."""
    assert RESEARCH_FILE.is_file(), (
        f"{RESEARCH_DIR}/{RESEARCH_PATTERNS_FILE} is missing. "
        f"If it moved, update RESEARCH_DIR in tools/spec_extract.py."
    )


def test_patterns_are_present() -> None:
    assert PATTERNS, "Research file contains no patterns — coverage rows would vanish silently"


@pytest.mark.parametrize("key", PATTERN_IDS)
def test_pattern_has_engine_mapping(key: str) -> None:
    """An unmapped pattern is dropped by the extractor and never tracked."""
    mapping = PATTERNS[key].get("engine_mapping")
    assert isinstance(mapping, dict), (
        f"Pattern '{key}' has no engine_mapping block. Without it the pattern is "
        f"skipped by extract_research_features() and never appears in the coverage matrix."
    )
    assert mapping.get("search_tokens"), f"Pattern '{key}' engine_mapping has no search_tokens"


@pytest.mark.parametrize("key", PATTERN_IDS)
def test_search_files_resolve(key: str) -> None:
    """Scope paths must exist, or the scan silently widens to the whole repo."""
    for rel in PATTERNS[key]["engine_mapping"].get("search_files", []):
        target = REPO_ROOT / rel
        assert target.exists(), (
            f"Pattern '{key}' scopes its scan to '{rel}', which does not exist. "
            f"scan_codebase() falls back to searching the entire repo, producing false evidence."
        )


@pytest.mark.parametrize("key", PATTERN_IDS)
def test_search_tokens_still_match_engine_code(key: str) -> None:
    """At least one token must appear in engine/ — catches renames."""
    tokens = PATTERNS[key]["engine_mapping"]["search_tokens"]
    engine_src = [
        p.read_text(encoding="utf-8", errors="replace").lower()
        for p in (REPO_ROOT / "engine").rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    matched = [t for t in tokens if any(t.lower() in src for src in engine_src)]
    assert matched, (
        f"Pattern '{key}' tokens {tokens} match nothing in engine/. "
        f"Either the capability was removed or a symbol was renamed — update engine_mapping."
    )


def test_extractor_emits_one_feature_per_mapped_pattern() -> None:
    """End-to-end: the wiring actually produces coverage rows."""
    features = extract_research_features(REPO_ROOT)
    mapped = [k for k, v in PATTERNS.items() if v.get("engine_mapping", {}).get("search_tokens")]

    assert len(features) == len(mapped), f"Expected {len(mapped)} research features, got {len(features)}"
    assert all(f.category == "research_pattern" for f in features), "Research features must share one category"

    for feature in features:
        assert feature.spec_reference.startswith(RESEARCH_DIR), (
            f"Feature '{feature.name}' spec_reference does not trace back to the research file"
        )


def test_extractor_tolerates_missing_file(tmp_path: Path) -> None:
    """A repo without the research file still produces a spec coverage report."""
    assert extract_research_features(tmp_path) == []
