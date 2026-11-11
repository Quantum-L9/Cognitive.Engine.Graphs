"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, api]
owner: engine-team
status: active
--- /L9_META ---

Tests for API error handling and HTTP status codes.

Verifies that handler failures return appropriate HTTP status codes
instead of always returning 200 (AUD9-2-HIGH-1, AUD9-7-MED-1).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from chassis.chassis_app import ChassisSettings, LifecycleHook, create_app

pytestmark = pytest.mark.unit


class MockLifecycleHook(LifecycleHook):
    """Test implementation of LifecycleHook for error handling tests."""

    def __init__(self):
        self._execute_mock = AsyncMock()

    async def startup(self) -> None:
        # Not implemented — test stub requires no startup logic
        pass

    async def shutdown(self) -> None:
        # Not implemented — test stub requires no shutdown logic
        pass

    async def execute(
        self,
        action: str,
        payload: dict,
        tenant: str,
        trace_id: str,
    ) -> dict:
        return await self._execute_mock(action, payload, tenant, trace_id)


@pytest.fixture
def mock_hook():
    """Create mock lifecycle hook."""
    return MockLifecycleHook()


@pytest.fixture
def test_client(mock_hook):
    """Create test client with mock lifecycle hook."""
    settings = ChassisSettings(cors_origins=[])
    app = create_app(lifecycle_hook=mock_hook, settings=settings)
    return TestClient(app)


class TestErrorHandling:
    """Tests for proper HTTP status code handling."""

    def test_unknown_action_returns_400(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Unknown action should return HTTP 400 when hook raises ValueError."""
        mock_hook._execute_mock.side_effect = ValueError("Unknown action: nonexistent_action")

        response = test_client.post(
            "/v1/execute",
            json={
                "action": "nonexistent_action",
                "tenant": "test",
                "payload": {},
            },
        )
        assert response.status_code == 400
        assert "Unknown action" in response.json()["detail"]

    def test_handler_failure_returns_500(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Handler execution failure should return HTTP 500, not 200."""
        mock_hook._execute_mock.return_value = {
            "status": "failed",
            "action": "match",
            "tenant": "test",
            "data": {"error": "Database connection failed"},
            "meta": {"trace_id": "test123"},
        }

        response = test_client.post(
            "/v1/execute",
            json={
                "action": "match",
                "tenant": "test",
                "payload": {"query": {}},
            },
        )

        assert response.status_code == 500
        assert "Database connection failed" in response.json()["detail"]

    def test_validation_error_returns_422(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Validation errors should return HTTP 422."""
        mock_hook._execute_mock.return_value = {
            "status": "failed",
            "action": "match",
            "tenant": "test",
            "data": {"error": "Invalid query: missing required field"},
            "meta": {"trace_id": "test123"},
        }

        response = test_client.post(
            "/v1/execute",
            json={
                "action": "match",
                "tenant": "test",
                "payload": {},
            },
        )

        assert response.status_code == 422
        assert "Invalid" in response.json()["detail"]

    def test_successful_action_returns_200(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Successful actions should return HTTP 200."""
        mock_hook._execute_mock.return_value = {
            "status": "success",
            "action": "health",
            "tenant": "test",
            "data": {"status": "healthy"},
            "meta": {"trace_id": "test123"},
        }

        response = test_client.post(
            "/v1/execute",
            json={
                "action": "health",
                "tenant": "test",
                "payload": {},
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"


class TestHealthEndpoint:
    """Tests for /v1/health endpoint."""

    def test_healthy_returns_200(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Healthy status should return HTTP 200."""
        mock_hook._execute_mock.return_value = {
            "status": "success",
            "action": "health",
            "tenant": "default",
            "data": {"status": "healthy"},
            "meta": {},
        }

        response = test_client.get("/v1/health")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "healthy"

    def test_unhealthy_returns_503(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Unhealthy status should return HTTP 503."""
        mock_hook._execute_mock.return_value = {
            "status": "success",
            "action": "health",
            "tenant": "default",
            "data": {"status": "unhealthy", "error": "Neo4j unreachable"},
            "meta": {},
        }

        response = test_client.get("/v1/health")

        assert response.status_code == 503

    def test_health_exception_returns_503(self, test_client: TestClient, mock_hook: MockLifecycleHook) -> None:
        """Health check exception should return HTTP 503."""
        mock_hook._execute_mock.side_effect = Exception("Connection refused")

        response = test_client.get("/v1/health")

        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


# ── RU-083 / CEG-COR-087 / CEG-COR-088 (RG-112) ─────────────────────────────
# Fake engine handlers routed through the real chassis.actions.execute_action,
# mirroring the planner repro r3_legacy_chassis.py / r3b_feature_flag.py.


class _ActionsHook(LifecycleHook):
    """LifecycleHook that delegates to chassis.actions.execute_action, as GraphLifecycle does."""

    async def startup(self) -> None:
        return

    async def shutdown(self) -> None:
        return

    async def execute(self, action: str, payload: dict, tenant: str, trace_id: str) -> dict:
        from chassis.actions import execute_action

        return await execute_action(action=action, payload=payload, tenant=tenant, trace_id=trace_id)


def _raiser(exc: Exception):
    async def handler(tenant: str, payload: dict) -> dict:
        raise exc

    return handler


async def _ok_handler(tenant: str, payload: dict) -> dict:
    return {"ok": True, "tenant": tenant}


@pytest.fixture
def actions_client(monkeypatch: pytest.MonkeyPatch):
    """TestClient over the legacy chassis with fake ACTION_HANDLERS; records persisted engine_data."""
    from chassis import actions
    from chassis.errors import FeatureNotEnabled
    from engine.graph.circuit_breaker import CircuitOpenError
    from engine.handlers import ExecutionError as EngineExecutionError
    from engine.handlers import ValidationError as EngineValidationError

    actions._init_engine()
    handlers = {
        "ok": _ok_handler,
        "circuit": _raiser(CircuitOpenError("neo4j", 12.5)),
        "feature": _raiser(FeatureNotEnabled("kge", flag="KGE_ENABLED", action="admin", tenant="t1")),
        "denied": _raiser(
            EngineValidationError("Tenant 'evil' not in authorized allowlist", action="match", tenant="evil")
        ),
        "engine_exec": _raiser(EngineExecutionError("Neo4j query failed", action="match", tenant="t1")),
        "internal": _raiser(
            RuntimeError("neo4j://admin:s3cret@10.0.0.5:7687 connection refused; query=MATCH (n:Secret) RETURN n")
        ),
    }
    monkeypatch.setattr(actions, "_engine_handlers", handlers)
    persisted: list[dict] = []
    real_deflate = actions._deflate_egress

    def recording_deflate(**kwargs):
        persisted.append({"status": kwargs["status"], "engine_data": dict(kwargs["engine_data"])})
        return real_deflate(**kwargs)

    monkeypatch.setattr(actions, "_deflate_egress", recording_deflate)
    app = create_app(lifecycle_hook=_ActionsHook(), settings=ChassisSettings(cors_origins=[]))
    client = TestClient(app)
    client.persisted = persisted  # type: ignore[attr-defined]
    return client


def _post(client: TestClient, action: str):
    return client.post("/v1/execute", json={"action": action, "tenant": "t1", "payload": {}})


class TestLegacyChassisStatusMapping:
    """RG-112: execute_action's failure class reaches the HTTP status (flag on, default)."""

    def test_success_unchanged(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "ok")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["data"] == {"ok": True, "tenant": "t1"}
        assert "http_status" not in body

    def test_circuit_open_returns_503_with_retry_after(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "circuit")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "13"
        assert "Circuit breaker 'neo4j' is OPEN" in response.json()["detail"]

    def test_feature_not_enabled_returns_422(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "feature")
        assert response.status_code == 422
        assert "KGE_ENABLED" in response.json()["detail"]

    def test_engine_validation_error_returns_422(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "denied")
        assert response.status_code == 422
        assert "not in authorized allowlist" in response.json()["detail"]

    def test_other_engine_error_returns_500_with_its_message(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "engine_exec")
        assert response.status_code == 500
        assert response.json()["detail"] == "Neo4j query failed"

    def test_unexpected_exception_returns_generic_500(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "internal")
        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"
        assert "s3cret" not in response.text
        assert "Secret" not in response.text

    def test_persisted_response_packet_keeps_the_error_detail(self, actions_client: TestClient) -> None:
        _post(actions_client, "internal")
        assert actions_client.persisted[-1]["status"] == "failed"  # type: ignore[attr-defined]
        assert "s3cret" in actions_client.persisted[-1]["engine_data"]["error"]  # type: ignore[attr-defined]
        assert "http_status" not in actions_client.persisted[-1]["engine_data"]  # type: ignore[attr-defined]

    def test_unknown_action_still_400(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "nope")
        assert response.status_code == 400
        assert "Unknown action" in response.json()["detail"]


class TestLegacyChassisStatusMappingFlagOff:
    """LEGACY_CHASSIS_STATUS_MAPPING_ENABLED=false restores the previous mapping."""

    @pytest.fixture(autouse=True)
    def _flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "legacy_chassis_status_mapping_enabled", False)

    def test_circuit_open_returns_200(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "circuit")
        assert response.status_code == 200
        assert response.json()["status"] == "circuit_open"
        assert response.json()["data"]["status_code"] == 503
        assert "http_status" not in response.json()

    def test_feature_not_enabled_returns_500(self, actions_client: TestClient) -> None:
        assert _post(actions_client, "feature").status_code == 500

    def test_unexpected_exception_detail_is_the_raw_message(self, actions_client: TestClient) -> None:
        response = _post(actions_client, "internal")
        assert response.status_code == 500
        assert "s3cret" in response.json()["detail"]
