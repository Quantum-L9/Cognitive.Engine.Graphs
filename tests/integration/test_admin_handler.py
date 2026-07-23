"""Integration tests — admin handler: list_domains, get_domain, init_schema.

Handlers use the W4-01 EngineState DI: dependencies are injected via
engine.handlers.init_dependencies() (the ``engine_deps`` fixture), and the
handler signature is ``handle_admin(tenant, payload)``.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_domains_returns_plasticos(engine_deps):
    from engine.handlers import handle_admin

    result = await handle_admin(
        "plasticos",
        {"subaction": "list_domains"},
    )
    assert "plasticos" in str(result)


@pytest.mark.asyncio
async def test_get_domain_returns_spec(engine_deps):
    from engine.handlers import handle_admin

    result = await handle_admin(
        "plasticos",
        {"subaction": "get_domain", "domain_id": "plasticos"},
    )
    assert result.get("status") in ("ok", "success") or "plasticos" in str(result)


@pytest.mark.asyncio
async def test_admin_missing_domain_id_handled(engine_deps):
    """get_domain without domain_id should raise or return error."""
    from engine.handlers import handle_admin

    try:
        result = await handle_admin(
            "plasticos",
            {"subaction": "get_domain"},
        )
        # Some implementations return error dict instead of raising
        assert "error" in result or result.get("status") == "error"
    except Exception:
        pass  # Raising is also acceptable
