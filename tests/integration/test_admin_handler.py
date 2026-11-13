"""Integration tests — admin handler: list_domains, get_domain, init_schema.

Handlers use the W4-01 EngineState DI: dependencies are injected via
engine.handlers.init_dependencies() (the ``engine_deps`` fixture), and the
handler signature is ``handle_admin(tenant, payload)``. Runs on the synthetic
orbital-logistics pack (ADR-112 Stage 4, A16).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

ORBITAL = "orbital-logistics"


@pytest.mark.asyncio
async def test_list_domains_returns_the_synthetic_packs(engine_deps):
    from engine.handlers import handle_admin

    result = await handle_admin(
        ORBITAL,
        {"subaction": "list_domains"},
    )
    assert ORBITAL in str(result)
    assert "cheese-aging" in str(result)


@pytest.mark.asyncio
async def test_get_domain_returns_spec(engine_deps):
    from engine.handlers import handle_admin

    result = await handle_admin(
        ORBITAL,
        {"subaction": "get_domain", "domain_id": ORBITAL},
    )
    assert result.get("status") in ("ok", "success") or ORBITAL in str(result)


@pytest.mark.asyncio
async def test_admin_missing_domain_id_handled(engine_deps):
    """get_domain without domain_id should raise or return error."""
    from engine.handlers import handle_admin

    raised = False
    result: dict = {}
    try:
        result = await handle_admin(
            ORBITAL,
            {"subaction": "get_domain"},
        )
    except Exception:
        raised = True  # Raising is also acceptable

    if not raised:
        # Some implementations return error dict instead of raising
        assert "error" in result or result.get("status") == "error"
