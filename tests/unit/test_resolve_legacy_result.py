"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, handlers, resolve]
owner: engine-team
status: active
--- /L9_META ---

RU-004 / CEG-COR-014: legacy resolve reports ``resolved`` only with a created edge.

The legacy (non-semantic-registry) path discarded the driver result and
returned ``status: resolved`` even when the source or target MATCH found
nothing and no RESOLVED_FROM edge was created.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.handlers import ExecutionError, ValidationError, handle_resolve, init_dependencies

DOMAINS = Path(__file__).resolve().parents[2] / "domains"
PAYLOAD = {"entity_type": "Facility", "source_id": "a", "target_id": "b"}


class _Rows:
    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows = rows

    async def execute_query(self, *, cypher: str, parameters: dict[str, Any], database: str) -> list[dict[str, Any]]:
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


def _wire(rows: list[dict[str, Any]] | Exception) -> None:
    loader = MagicMock()
    loader.load_domain.return_value = DomainPackLoader(config_path=str(DOMAINS)).load_domain("plasticos")
    init_dependencies(_Rows(rows), loader)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-014")
class TestLegacyResolveResult:
    async def test_no_row_raises_validation_error(self) -> None:
        _wire([])
        with pytest.raises(ValidationError, match="source or target entity not found for Facility"):
            await handle_resolve("plasticos", dict(PAYLOAD))

    async def test_created_row_reports_resolved(self) -> None:
        _wire([{"resolution_id": "res_x"}])
        result = await handle_resolve("plasticos", dict(PAYLOAD))
        assert result["status"] == "resolved"
        assert result["source_id"] == "a"
        assert result["target_id"] == "b"

    async def test_driver_failure_still_execution_error(self) -> None:
        _wire(RuntimeError("boom"))
        with pytest.raises(ExecutionError):
            await handle_resolve("plasticos", dict(PAYLOAD))

    async def test_flag_off_restores_unconditional_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "resolve_requires_created_edge", False)
        _wire([])
        result = await handle_resolve("plasticos", dict(PAYLOAD))
        assert result["status"] == "resolved"
