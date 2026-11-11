"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api, auth, test]
tags: [chassis, auth, test]
owner: platform-team
status: active
--- /L9_META ---

tests/unit/test_chassis_auth_middleware.py
Tests for BearerAuthMiddleware and its registration on the legacy chassis.

RU-082 / CEG-COR-039 (RG-110): this file used to live at
chassis/auth/test_auth_middleware.py, outside pytest's testpaths, and imported
the nonexistent ``engine.api.auth``, so it was never collected. It now imports
``chassis.auth.auth`` and also covers the legacy ``chassis_app.create_app``
factory: the environment guard, conditional auth registration
(``LEGACY_CHASSIS_AUTH_ENABLED`` + ``L9_API_KEY``) and the W3-01
``allowed_tenants`` comparison on POST /v1/execute.

Covers:
- Valid key passes through to handler
- Missing Authorization header returns 401
- Malformed Authorization header returns 401
- Invalid/wrong token returns 403
- Health endpoint passes without any key
- OPTIONS (CORS preflight) passes without key
- Empty L9_API_KEY rejects all authenticated requests
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from chassis.auth.auth import BearerAuthMiddleware
from chassis.chassis_app import ChassisSettings, LifecycleHook, create_app
from engine.config.settings import Settings, settings

pytestmark = pytest.mark.unit

# ── Test fixtures ────────────────────────────────────────────

TEST_API_KEY = "test-key-abc123-secure-token-xyz789"


def _build_test_app(api_key: str = TEST_API_KEY) -> FastAPI:
    """Build minimal FastAPI app with auth middleware for testing."""
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, api_key=api_key)

    @app.post("/v1/execute")
    async def execute() -> JSONResponse:
        return JSONResponse(content={"status": "success", "action": "test"})

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"status": "healthy"})

    @app.get("/docs")
    async def docs() -> JSONResponse:
        return JSONResponse(content={"docs": True})

    return app


@pytest.fixture
def app() -> FastAPI:
    return _build_test_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Valid key passes ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_key_passes(client: AsyncClient) -> None:
    """Request with correct Bearer token reaches the handler."""
    response = await client.post(
        "/v1/execute",
        json={"action": "health", "tenant": "test"},
        headers={"Authorization": f"Bearer {TEST_API_KEY}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.asyncio
async def test_valid_key_case_insensitive_scheme(client: AsyncClient) -> None:
    """Bearer scheme matching is case-insensitive per RFC 7235."""
    response = await client.post(
        "/v1/execute",
        json={},
        headers={"Authorization": f"bearer {TEST_API_KEY}"},
    )
    assert response.status_code == 200


# ── Missing key → 401 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401(client: AsyncClient) -> None:
    """No Authorization header → 401 Unauthorized."""
    response = await client.post("/v1/execute", json={})
    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "error"
    assert "Missing" in body["detail"]


# ── Malformed header → 401 ───────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_auth_no_scheme(client: AsyncClient) -> None:
    """Authorization header without scheme → 401."""
    response = await client.post(
        "/v1/execute",
        json={},
        headers={"Authorization": TEST_API_KEY},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_auth_wrong_scheme(client: AsyncClient) -> None:
    """Non-Bearer scheme → 401."""
    response = await client.post(
        "/v1/execute",
        json={},
        headers={"Authorization": f"Basic {TEST_API_KEY}"},
    )
    assert response.status_code == 401


# ── Bad key → 403 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_key_returns_403(client: AsyncClient) -> None:
    """Incorrect Bearer token → 403 Forbidden."""
    response = await client.post(
        "/v1/execute",
        json={},
        headers={"Authorization": "Bearer wrong-key-definitely-not-valid"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["status"] == "error"
    assert "Invalid" in body["detail"]


@pytest.mark.asyncio
async def test_empty_bearer_token_returns_403(client: AsyncClient) -> None:
    """Empty token after Bearer → 403."""
    response = await client.post(
        "/v1/execute",
        json={},
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 403


# ── Health endpoint needs no key ─────────────────────────────


@pytest.mark.asyncio
async def test_health_no_auth_required(client: AsyncClient) -> None:
    """/v1/health is public — no Authorization header needed."""
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_with_trailing_slash(client: AsyncClient) -> None:
    """/v1/health/ (trailing slash) is also public.

    The middleware passes it without a key; FastAPI then redirects (307) to the
    declared /v1/health route, so the redirect is followed (RU-082: this file was
    never collected before, and asserted 200 on the unfollowed redirect).
    """
    response = await client.get("/v1/health/", follow_redirects=True)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_docs_no_auth_required(client: AsyncClient) -> None:
    """/docs is public."""
    response = await client.get("/docs")
    assert response.status_code == 200


# ── OPTIONS (CORS preflight) ────────────────────────────────


@pytest.mark.asyncio
async def test_options_preflight_no_auth(client: AsyncClient) -> None:
    """CORS preflight OPTIONS requests skip auth."""
    response = await client.options("/v1/execute")
    # FastAPI returns 405 for unhandled OPTIONS, but NOT 401/403
    assert response.status_code != 401
    assert response.status_code != 403


# ── Empty API key rejects everything ─────────────────────────


@pytest.mark.asyncio
async def test_empty_api_key_rejects_all() -> None:
    """When L9_API_KEY is empty, all authenticated endpoints return 403."""
    app = _build_test_app(api_key="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/execute",
            json={},
            headers={"Authorization": "Bearer some-token"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_empty_api_key_health_still_public() -> None:
    """Even with empty L9_API_KEY, health endpoint stays public."""
    app = _build_test_app(api_key="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/health")
        assert response.status_code == 200


# ── Legacy chassis factory (RU-082) ──────────────────────────


def _jwt_key(claims: dict[str, Any]) -> str:
    """A JWT-shaped API key whose payload segment carries ``claims``."""

    def seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


class _EchoHook(LifecycleHook):
    async def startup(self) -> None:
        return

    async def shutdown(self) -> None:
        return

    async def execute(self, action: str, payload: dict, tenant: str, trace_id: str) -> dict:
        data = {"status": "healthy"} if action == "health" else {"executed_for_tenant": tenant}
        return {"status": "success", "action": action, "tenant": tenant, "data": data, "meta": {}}


ACME_KEY = _jwt_key({"sub": "svc", "allowed_tenants": ["acme"]})
BYPASS_KEY = "bypass-secret-value"


def _legacy_client(monkeypatch: pytest.MonkeyPatch, api_key: str = ACME_KEY) -> AsyncClient:
    monkeypatch.setattr(settings, "tenant_auth_bypass_key", BYPASS_KEY)
    app = create_app(lifecycle_hook=_EchoHook(), settings=ChassisSettings(cors_origins=[], l9_api_key=api_key))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _execute_body(tenant: str) -> dict[str, Any]:
    return {"action": "match", "tenant": tenant, "payload": {}}


@pytest.mark.asyncio
async def test_legacy_execute_without_authorization_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _legacy_client(monkeypatch) as client:
        response = await client.post("/v1/execute", json=_execute_body("acme"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_legacy_execute_with_wrong_key_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _legacy_client(monkeypatch) as client:
        response = await client.post(
            "/v1/execute", json=_execute_body("acme"), headers={"Authorization": "Bearer not-the-key"}
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_legacy_execute_allowed_tenant_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _legacy_client(monkeypatch) as client:
        response = await client.post(
            "/v1/execute", json=_execute_body("acme"), headers={"Authorization": f"Bearer {ACME_KEY}"}
        )
    assert response.status_code == 200
    assert response.json()["data"] == {"executed_for_tenant": "acme"}


@pytest.mark.asyncio
async def test_legacy_execute_tenant_outside_token_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """W3-01: before RU-082 this returned 200 and executed for 'globex'."""
    async with _legacy_client(monkeypatch) as client:
        response = await client.post(
            "/v1/execute", json=_execute_body("globex"), headers={"Authorization": f"Bearer {ACME_KEY}"}
        )
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"]


@pytest.mark.asyncio
async def test_legacy_execute_refused_tenant_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The refused tenant is request text: a CR/LF in it must not forge a log line."""
    forged = "globex\nINFO forged audit line"
    with caplog.at_level("WARNING"):
        async with _legacy_client(monkeypatch) as client:
            response = await client.post(
                "/v1/execute", json=_execute_body(forged), headers={"Authorization": f"Bearer {ACME_KEY}"}
            )
    assert response.status_code == 403
    assert any("allowed_tenants" in r.getMessage() for r in caplog.records)
    assert "forged audit line" not in caplog.text


@pytest.mark.asyncio
async def test_legacy_execute_tenant_mismatch_with_bypass_key_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _legacy_client(monkeypatch) as client:
        response = await client.post(
            "/v1/execute",
            json=_execute_body("globex"),
            headers={"Authorization": f"Bearer {ACME_KEY}", "X-Internal-Bypass-Key": BYPASS_KEY},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_legacy_execute_tenant_mismatch_allowed_when_tenant_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tenant_auth_enabled", False)
    async with _legacy_client(monkeypatch) as client:
        response = await client.post(
            "/v1/execute", json=_execute_body("globex"), headers={"Authorization": f"Bearer {ACME_KEY}"}
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_legacy_execute_string_claim_is_not_a_substring_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """allowed_tenants='acme-corp' (a string) authorizes 'acme-corp', never 'acme'."""
    key = _jwt_key({"allowed_tenants": "acme-corp"})
    async with _legacy_client(monkeypatch, api_key=key) as client:
        ok = await client.post(
            "/v1/execute", json=_execute_body("acme-corp"), headers={"Authorization": f"Bearer {key}"}
        )
        bad = await client.post("/v1/execute", json=_execute_body("acme"), headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_legacy_health_is_public_with_auth_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _legacy_client(monkeypatch) as client:
        response = await client.get("/v1/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_legacy_auth_not_registered_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """RG-111 guard: no L9_API_KEY configured -> no auth, keyless dev keeps working."""
    async with _legacy_client(monkeypatch, api_key="") as client:
        response = await client.post("/v1/execute", json=_execute_body("anyone"))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_legacy_auth_flag_off_restores_unauthenticated_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "legacy_chassis_auth_enabled", False)
    async with _legacy_client(monkeypatch) as client:
        response = await client.post("/v1/execute", json=_execute_body("globex"))
    assert response.status_code == 200


def test_legacy_factory_refuses_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launching chassis.chassis_app:create_app directly cannot bypass the entrypoint guard."""
    prod = Settings(
        l9_env="prod", neo4j_password="test-pw", api_secret_key="test-key", require_sdk_chassis_in_prod=True
    )
    monkeypatch.setattr("engine.config.settings.settings", prod)
    with pytest.raises(ValueError, match="must be 'sdk'"):
        create_app(lifecycle_hook=_EchoHook(), settings=ChassisSettings(cors_origins=[]))


def test_legacy_factory_builds_in_prod_when_guard_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    prod = Settings(
        l9_env="prod", neo4j_password="test-pw", api_secret_key="test-key", require_sdk_chassis_in_prod=False
    )
    monkeypatch.setattr("engine.config.settings.settings", prod)
    assert isinstance(create_app(lifecycle_hook=_EchoHook(), settings=ChassisSettings(cors_origins=[])), FastAPI)
