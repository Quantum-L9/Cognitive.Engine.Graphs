"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api]
tags: [chassis, fastapi, single-ingress]
owner: platform-team
status: active
--- /L9_META ---

chassis/chassis_app.py — Reusable, Micro-Service Agnostic L9 Chassis

Single-ingress HTTP boundary for ANY L9 constellation engine.
Zero engine imports.  All engine coupling flows through two seams:

    1. LifecycleHook — engine tells the chassis how to start/stop.
    2. execute_action  — chassis calls the engine's action router.

Usage (Graph engine):
    # in your engine's __main__.py or entrypoint:
    from chassis.chassis_app import create_app
    from myengine.boot import GraphLifecycle

    app = create_app(lifecycle_hook=GraphLifecycle())

Usage (uvicorn --factory):
    # set L9_LIFECYCLE_HOOK=myengine.boot:GraphLifecycle  (env var)
    uvicorn chassis.chassis_app:create_app --factory
"""

from __future__ import annotations

import importlib
import logging
import math
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from chassis.actions import HTTP_DETAIL_KEY as _HTTP_DETAIL_KEY
from chassis.actions import HTTP_RETRY_AFTER_KEY as _HTTP_RETRY_AFTER_KEY
from chassis.actions import HTTP_STATUS_KEY as _HTTP_STATUS_KEY

# Engine ValidationError maps to 422 — import lazily to avoid circular dep
try:
    from engine.handlers import ValidationError as _EngineValidationError
except ImportError:  # pragma: no cover
    _EngineValidationError = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  CHASSIS-OWNED CONFIGURATION  (engine never touches this)
# ═══════════════════════════════════════════════════════════════════════════


class ChassisSettings(BaseSettings):
    """
    Minimal config the chassis itself needs.
    Engine-specific settings live in the engine's own Settings class.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- API surface ---
    api_title: str = "L9 Engine"
    api_description: str = "Domain-agnostic L9 chassis"
    api_version: str = "1.1.0"

    # --- CORS ---
    cors_origins: list[str] = []

    # --- Lifecycle hook (dotted path  module.path:ClassName) ---
    l9_lifecycle_hook: str = ""

    # --- API authentication (RU-082) ---
    # L9_API_KEY. When set (and legacy_chassis_auth_enabled), create_app
    # registers BearerAuthMiddleware; when empty the legacy chassis is auth-free
    # (it cannot start outside dev/local/test, see chassis.entrypoint).
    l9_api_key: str = ""


_chassis_settings = ChassisSettings()


# ═══════════════════════════════════════════════════════════════════════════
#  CHASSIS-OWNED ENVELOPE MODELS
# ═══════════════════════════════════════════════════════════════════════════


class ExecuteRequest(BaseModel):
    """Universal execute request envelope — chassis contract."""

    action: str
    tenant: str
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ExecuteResponse(BaseModel):
    """Universal execute response envelope — chassis contract."""

    status: str
    action: str
    tenant: str
    data: dict[str, Any]
    meta: dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════
#  LIFECYCLE HOOK — the engine's ONLY coupling surface to the chassis
# ═══════════════════════════════════════════════════════════════════════════


class LifecycleHook(ABC):
    """
    Abstract contract that every L9 engine implements ONCE.

    The chassis calls:
        startup()   — engine wires its own drivers, loaders, schedulers
        shutdown()  — engine tears down connections
        execute()   — chassis forwards every /v1/execute payload here

    This is the single integration seam.  The chassis has ZERO knowledge
    of Neo4j, DomainPackLoader, GraphDriver, or any engine internals.
    """

    @abstractmethod
    async def startup(self) -> None:
        """Engine-specific initialization (connect DB, load domains, etc.)."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Engine-specific teardown (close pools, flush queues, etc.)."""

    @abstractmethod
    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        tenant: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """
        Execute an action and return the canonical envelope dict:
            {status, action, tenant, data, meta}
        """

    async def health(self, tenant: str, trace_id: str) -> dict[str, Any]:
        """
        Optional override.  Default delegates to execute(action="health").
        Engines that want a cheaper probe can override this directly.
        """
        return await self.execute(
            action="health",
            payload={},
            tenant=tenant,
            trace_id=trace_id,
        )


class _NoOpLifecycle(LifecycleHook):
    """Fallback when no hook is provided — passes smoke tests, nothing else."""

    async def startup(self) -> None:
        logger.warning("No LifecycleHook configured — chassis running in stub mode")

    async def shutdown(self) -> None:
        # Not implemented — no cleanup required for stub mode
        pass

    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        tenant: str,
        trace_id: str,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "action": action,
            "tenant": tenant,
            "data": {"error": "No engine lifecycle hook configured"},
            "meta": {"trace_id": trace_id},
        }


# ═══════════════════════════════════════════════════════════════════════════
#  HOOK RESOLUTION  (env var → importlib → instance)
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_hook(hook: LifecycleHook | None) -> LifecycleHook:
    """
    Priority:
        1. Explicit instance passed to create_app()
        2. L9_LIFECYCLE_HOOK env var  (e.g. "myengine.boot:GraphLifecycle")
        3. _NoOpLifecycle fallback
    """
    if hook is not None:
        return hook

    dotted = _chassis_settings.l9_lifecycle_hook or os.getenv("L9_LIFECYCLE_HOOK", "")
    if dotted:
        try:
            module_path, class_name = dotted.rsplit(":", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            instance = cls()
            logger.info("Resolved LifecycleHook from %s → %s", dotted, type(instance).__name__)
            return instance
        except Exception:
            logger.exception("Failed to resolve L9_LIFECYCLE_HOOK=%s", dotted)
            raise

    return _NoOpLifecycle()


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTE HANDLER HELPERS  (extracted to reduce create_app cognitive complexity)
# ═══════════════════════════════════════════════════════════════════════════


def _raise_for_failed_result(result: dict[str, Any]) -> None:
    """Map engine 'failed' status to the correct HTTPException.

    Status-code mapping:
        422 — validation/invalid keyword in error OR ValueError/TypeError exception
        500 — all other failures
    """
    error_detail = result.get("data", {}).get("error", "Handler execution failed")
    exc_obj = result.get("data", {}).get("_exc")

    if exc_obj is not None:
        status = getattr(exc_obj, "status_code", None) or (422 if isinstance(exc_obj, (ValueError, TypeError)) else 500)
    else:
        # No exception object — infer from error message keywords
        _msg_lower = error_detail.lower() if isinstance(error_detail, str) else ""
        status = 422 if any(k in _msg_lower for k in ("validation", "invalid", "missing", "required")) else 500
    raise HTTPException(status_code=status, detail=error_detail)


# Results whose execute_action status hint becomes the HTTP status (RU-083).
_HINTED_STATUSES = frozenset({"failed", "circuit_open"})


def _raise_for_status_hint(hint: dict[str, Any]) -> None:
    """Raise the HTTPException named by execute_action's status hint (RU-083)."""
    status = hint[_HTTP_STATUS_KEY]
    detail = hint.get(_HTTP_DETAIL_KEY) or "Internal server error"
    headers: dict[str, str] | None = None
    retry_after = hint.get(_HTTP_RETRY_AFTER_KEY)
    if status == 503 and isinstance(retry_after, (int, float)):
        headers = {"Retry-After": str(max(1, math.ceil(retry_after)))}
    raise HTTPException(status_code=status, detail=detail, headers=headers)


async def _execute_route(request: ExecuteRequest, hook: LifecycleHook) -> ExecuteResponse | JSONResponse:
    """Execute handler body — delegates to the lifecycle hook."""
    trace_id = request.trace_id or f"trace_{uuid.uuid4().hex[:12]}"
    try:
        result = await hook.execute(
            action=request.action,
            payload=request.payload,
            tenant=request.tenant,
            trace_id=trace_id,
        )
        hint = {
            key: result.pop(key) for key in (_HTTP_STATUS_KEY, _HTTP_DETAIL_KEY, _HTTP_RETRY_AFTER_KEY) if key in result
        }
        if hint.get(_HTTP_STATUS_KEY) is not None and result.get("status") in _HINTED_STATUSES:
            from engine.config.settings import settings as engine_settings

            if engine_settings.legacy_chassis_status_mapping_enabled:
                _raise_for_status_hint(hint)
        if result.get("status") == "failed":
            _raise_for_failed_result(result)
        return ExecuteResponse(**result)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Execute failed: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error") from exc


def _enforce_token_tenant(tenant: str, http_request: Request) -> None:
    """W3-01: refuse a tenant outside the bearer token's ``allowed_tenants``.

    ``BearerAuthMiddleware`` decodes the claim onto ``request.state`` (it cannot
    read the body); the comparison happens here, after the ExecuteRequest is
    parsed (RU-082). No claim on the request (auth not registered, a token
    without the claim, or a valid service bypass key) means no restriction.
    A string claim names one tenant; any other non-list claim is refused.
    """
    allowed = getattr(http_request.state, "allowed_tenants", None)
    if allowed is None:
        return
    from engine.config.settings import settings as engine_settings

    if not engine_settings.tenant_auth_enabled:
        return
    if isinstance(allowed, str):
        allowed = [allowed]
    if not isinstance(allowed, (list, tuple, set, frozenset)) or tenant not in allowed:
        # The tenant is caller-supplied request body text: never write it to the
        # log (log injection, S5145); the 403 is the caller's evidence.
        logger.warning("Tenant not in token allowed_tenants; request refused")
        raise HTTPException(status_code=403, detail="Tenant not authorized for this API key")


async def _health_route(request: Request, hook: LifecycleHook) -> JSONResponse:
    """Health handler body — delegates to the lifecycle hook."""
    tenant = request.query_params.get("tenant", "default")
    trace_id = f"health_{uuid.uuid4().hex[:8]}"
    try:
        result = await hook.health(tenant=tenant, trace_id=trace_id)
        # RU-083 status hints are for /v1/execute only; keep the health body as before.
        for key in (_HTTP_STATUS_KEY, _HTTP_DETAIL_KEY, _HTTP_RETRY_AFTER_KEY):
            result.pop(key, None)
        status_code = 200 if result.get("data", {}).get("status") == "healthy" else 503
        return JSONResponse(content=result, status_code=status_code)
    except Exception as exc:
        logger.exception("Health check failed: %s", exc)
        return JSONResponse(
            content={"status": "unhealthy", "error": "health_check_failed"},
            status_code=503,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  APPLICATION FACTORY
# ═══════════════════════════════════════════════════════════════════════════


def create_app(
    *,
    lifecycle_hook: LifecycleHook | None = None,
    settings: ChassisSettings | None = None,
) -> FastAPI:
    """
    Factory function for the L9 chassis.

    Parameters
    ----------
    lifecycle_hook : LifecycleHook | None
        Engine-supplied hook.  If None, resolved from L9_LIFECYCLE_HOOK env.
    settings : ChassisSettings | None
        Override chassis settings (useful for testing).  Defaults to module-level singleton.
    """
    # RU-082: the factory enforces the entrypoint's environment guard itself,
    # so `uvicorn chassis.chassis_app:create_app --factory` cannot bypass it.
    from chassis.entrypoint import legacy_chassis_refusal
    from engine.config.settings import settings as engine_settings

    refusal = legacy_chassis_refusal()
    if refusal is not None:
        msg = f"Legacy chassis factory refused: {refusal}"
        raise ValueError(msg)

    cfg = settings or _chassis_settings
    hook = _resolve_hook(lifecycle_hook)

    # --- lifespan -----------------------------------------------------------

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("Chassis starting — lifecycle: %s", type(hook).__name__)
        await hook.startup()
        logger.info("Chassis ready")
        yield
        logger.info("Chassis shutting down…")
        await hook.shutdown()
        logger.info("Chassis shutdown complete")

    # --- app ----------------------------------------------------------------

    application = FastAPI(
        title=cfg.api_title,
        description=cfg.api_description,
        version=cfg.api_version,
        lifespan=lifespan,
    )

    # --- Authentication (conditional, RU-082) ---------------------------------
    # Registered before CORS so CORS stays the outermost middleware.

    if engine_settings.legacy_chassis_auth_enabled and cfg.l9_api_key:
        from chassis.auth.auth import BearerAuthMiddleware

        application.add_middleware(
            BearerAuthMiddleware,
            api_key=cfg.l9_api_key,
            tenant_auth_enabled=engine_settings.tenant_auth_enabled,
            tenant_auth_bypass_key=engine_settings.tenant_auth_bypass_key,
        )

    # --- CORS (conditional) -------------------------------------------------

    if cfg.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=False,
            allow_methods=["POST", "GET"],
            allow_headers=["*"],
        )

    # --- POST /v1/execute  (single ingress) ---------------------------------

    @application.post(
        "/v1/execute",
        response_model=ExecuteResponse,
        responses={
            400: {"description": "Unknown or invalid action (ValueError from engine)"},
            401: {"description": "Missing or malformed Authorization header (L9_API_KEY configured)"},
            403: {"description": "Invalid API key, or tenant not in the token's allowed_tenants"},
            422: {"description": "Payload validation failure"},
            500: {"description": "Unhandled engine error"},
            503: {"description": "Circuit breaker open (Retry-After header)"},
        },
    )
    async def execute(request: ExecuteRequest, http_request: Request) -> ExecuteResponse | JSONResponse:
        """
        Universal action endpoint — single ingress for every engine action.

        Status-code mapping:
            200  — success
            400  — unknown / invalid action  (ValueError from engine)
            401  — missing/malformed Authorization (when L9_API_KEY is configured)
            403  — wrong API key, or tenant outside the token's allowed_tenants
            422  — payload validation failure (engine ValidationError,
                   FeatureNotEnabled / ChassisError status_code)
            500  — engine error (its message) or unexpected error
                   ("Internal server error")
            503  — circuit breaker open, with Retry-After
        RU-083 (LEGACY_CHASSIS_STATUS_MAPPING_ENABLED): the status comes from
        execute_action's hint; off restores the keyword mapping.
        """
        _enforce_token_tenant(request.tenant, http_request)
        return await _execute_route(request, hook)

    # --- GET /v1/health  (unauthenticated) ----------------------------------

    @application.get("/v1/health")
    async def health(request: Request) -> JSONResponse:
        """
        Health probe.  Delegates to hook.health() so the engine controls
        what "healthy" means.  Kubernetes-compatible: 200 = live, 503 = not.
        """
        return await _health_route(request, hook)

    return application


# ═══════════════════════════════════════════════════════════════════════════
#  NOTE:  uvicorn with --factory flag:
#    uvicorn chassis.chassis_app:create_app --factory
#
#  To wire a specific engine, either:
#    1. Set env:   L9_LIFECYCLE_HOOK=engine.boot:GraphLifecycle
#    2. Or call:   create_app(lifecycle_hook=GraphLifecycle())
# ═══════════════════════════════════════════════════════════════════════════
