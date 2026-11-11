"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, ci]
owner: engine-team
status: active
--- /L9_META ---

The collection guard in tests/conftest.py: every test carries a classifying
marker, so `make test` (`pytest -m unit`) never silently skips one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import CLASSIFYING_MARKERS, unclassified_nodeids

pytestmark = pytest.mark.unit


def _item(nodeid: str, *markers: str) -> Any:
    names = set(markers)
    return SimpleNamespace(nodeid=nodeid, get_closest_marker=lambda name: name if name in names else None)


def test_classifying_markers_are_the_three_selection_axes() -> None:
    assert frozenset({"unit", "integration", "performance"}) == CLASSIFYING_MARKERS


@pytest.mark.parametrize("marker", sorted(CLASSIFYING_MARKERS))
def test_a_classified_test_passes(marker: str) -> None:
    assert unclassified_nodeids([_item("tests/x.py::t", marker)]) == []


def test_an_unclassified_test_is_reported_even_with_other_markers() -> None:
    items = [_item("tests/a.py::ok", "unit"), _item("tests/b.py::bare", "security", "finding")]
    assert unclassified_nodeids(items) == ["tests/b.py::bare"]
