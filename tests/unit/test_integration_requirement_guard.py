"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, integration-guard, ci]
owner: engine-team
status: active
--- /L9_META ---

tests/unit/test_integration_requirement_guard.py

Unit tests for the CEG_REQUIRE_INTEGRATION guard in tests/conftest.py.

CI's integration job runs tests/integration against real Neo4j Enterprise and
PostgreSQL services with CEG_REQUIRE_INTEGRATION=1. Under that flag a missing
service must FAIL the job instead of skipping it, otherwise the job reports
green while exercising nothing. Without the flag, skip behaviour is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import conftest as root_conftest

pytestmark = pytest.mark.unit

_ENV = root_conftest.REQUIRE_INTEGRATION_ENV


def _skipped_report(path: Path, *, wasxfail: str | None = None) -> pytest.TestReport:
    report = pytest.TestReport(
        nodeid=f"{path.name}::test_x",
        location=(str(path), 1, "test_x"),
        keywords={},
        outcome="skipped",
        longrepr=(str(path), 1, "Skipped: neo4j unavailable"),
        when="setup",
    )
    if wasxfail is not None:
        report.wasxfail = wasxfail
    return report


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_integration_required_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_ENV, value)
    assert root_conftest.integration_required() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "2"])
def test_integration_required_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_ENV, value)
    assert root_conftest.integration_required() is False


def test_integration_required_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert root_conftest.integration_required() is False


def test_unavailable_service_skips_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="NEO4J_TEST_URI not set"):
        root_conftest.skip_or_fail_unavailable("NEO4J_TEST_URI not set")


def test_unavailable_service_fails_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=r"CEG_REQUIRE_INTEGRATION=1 but NEO4J_TEST_URI not set"):
        root_conftest.skip_or_fail_unavailable("NEO4J_TEST_URI not set")


def test_integration_path_classification() -> None:
    integration_file = root_conftest.INTEGRATION_DIR / "test_match_handler.py"
    unit_file = Path(__file__)
    assert root_conftest._is_integration_path(integration_file) is True
    assert root_conftest._is_integration_path(unit_file) is False


def test_skip_report_is_rewritten_to_failure() -> None:
    report = _skipped_report(root_conftest.INTEGRATION_DIR / "test_match_handler.py")
    root_conftest._fail_required_skip(report)
    assert report.outcome == "failed"
    assert "CEG_REQUIRE_INTEGRATION=1 forbids skipping integration tests" in str(report.longrepr)
    assert "neo4j unavailable" in str(report.longrepr)


def test_xfail_report_is_left_alone() -> None:
    report = _skipped_report(root_conftest.INTEGRATION_DIR / "test_match_handler.py", wasxfail="known defect")
    root_conftest._fail_required_skip(report)
    assert report.outcome == "skipped"
