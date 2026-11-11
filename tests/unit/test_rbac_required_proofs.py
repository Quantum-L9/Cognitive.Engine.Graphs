"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, capability, auth]
owner: engine-team
status: active
--- /L9_META ---

docs/contracts/auth/rbac-policy.yaml required_proof items that were partial:

- "write-authorized caller can resolve in correct tenant" (production mode)
- "existing match permission not expanded"

Production mode is ``l9_env=prod``: dev, local and test bypass the capability
check (``_CAPABILITY_AUTH_BYPASS_ENVS``), so a grant is only exercised in prod.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.auth.capabilities import ACTION_PERMISSION_MAP
from engine.config.schema import CapabilitySpec
from engine.config.settings import settings

pytestmark = pytest.mark.unit

_TENANT = "orbital-logistics"
_OTHER = "consumer-a"
_WRITE_ACTIONS = ("sync", "outcomes", "resolve", "enrich", "admin")


def _wire(capabilities: list[CapabilitySpec], rows: list[dict[str, Any]] | None = None) -> tuple[MagicMock, Any]:
    from engine.handlers import init_dependencies
    from engine.state import get_state
    from tests.fixtures.domain_contracts import synthetic_loader

    spec = synthetic_loader().load_domain(_TENANT).model_copy(update={"capabilities": capabilities})
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=rows or [])
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [_TENANT]
    init_dependencies(driver, loader)
    get_state()._tenant_allowlist = {_TENANT, _OTHER}
    return driver, spec


@pytest.fixture(autouse=True)
def _production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "l9_env", "prod")
    monkeypatch.setattr(settings, "capability_auth_enabled", True)


def _resolve_payload(spec: Any) -> dict[str, Any]:
    return {"entity_type": spec.ontology.nodes[0].label, "source_id": "dup-1", "target_id": "canon-1"}


class TestWriteAuthorizedResolveInCorrectTenant:
    async def test_write_grant_resolves_in_the_callers_own_database(self) -> None:
        from engine.handlers import handle_resolve

        driver, spec = _wire(
            [CapabilitySpec(name="writers", actions=["sync:write"], allowed_subjects=[_TENANT])],
            rows=[{"resolution_id": "res_x"}],
        )
        result = await handle_resolve(_TENANT, _resolve_payload(spec))

        assert result["status"] == "resolved"
        driver.execute_query.assert_awaited_once()
        kwargs = driver.execute_query.await_args.kwargs
        assert kwargs["database"] == spec.domain.id == _TENANT
        assert kwargs["parameters"]["tenant"] == _TENANT

    async def test_a_write_grant_naming_another_tenant_does_not_authorize(self) -> None:
        from engine.handlers import ValidationError, handle_resolve

        driver, spec = _wire([CapabilitySpec(name="writers", actions=["sync:write"], allowed_subjects=[_OTHER])])
        with pytest.raises(ValidationError, match="lacks capability 'sync:write'"):
            await handle_resolve(_TENANT, _resolve_payload(spec))
        driver.execute_query.assert_not_awaited()


class TestMatchPermissionNotExpanded:
    def test_match_requires_exactly_match_read(self) -> None:
        assert ACTION_PERMISSION_MAP["match"] == "match:read"

    def test_no_write_action_is_satisfied_by_match_read(self) -> None:
        assert all(ACTION_PERMISSION_MAP[action] != "match:read" for action in _WRITE_ACTIONS)
        assert all(not ACTION_PERMISSION_MAP[action].endswith(":read") for action in _WRITE_ACTIONS)

    @pytest.mark.parametrize("action", _WRITE_ACTIONS)
    def test_a_match_read_grant_authorizes_match_and_no_write_action(self, action: str) -> None:
        from engine.handlers import ValidationError, _enforce_capability

        _, spec = _wire([CapabilitySpec(name="readers", actions=["match:read"], allowed_subjects=[_TENANT])])
        _enforce_capability(_TENANT, "match", spec)
        with pytest.raises(ValidationError, match="lacks capability"):
            _enforce_capability(_TENANT, action, spec)
