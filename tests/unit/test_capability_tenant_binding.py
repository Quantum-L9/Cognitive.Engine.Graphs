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

CAP-TENANT-01 (W2): admin revoke_capability and capability_audit are bound to
the packet tenant. Contract: docs/contracts/auth/rbac-policy.yaml required_proof
"revoke/capability_audit restricted to authorized operator".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.auth.capabilities import Capability, get_capability_validator, reset_capability_validator
from engine.config.schema import CapabilitySpec
from engine.config.settings import settings

pytestmark = pytest.mark.unit

_CALLER = "orbital-logistics"
_OTHER = "consumer-a"
_UNKNOWN_MESSAGE = "not found in registry"


@pytest.fixture(autouse=True)
def _wired() -> Any:
    from engine.handlers import init_dependencies
    from engine.state import get_state
    from tests.fixtures.domain_contracts import synthetic_loader

    reset_capability_validator()
    spec = synthetic_loader().load_domain(_CALLER)
    spec = spec.model_copy(
        update={"capabilities": [CapabilitySpec(name="ops", actions=["admin:write"], allowed_subjects=[_CALLER])]}
    )
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[])
    loader = MagicMock()
    loader.load_domain.return_value = spec
    loader.list_domains.return_value = [_CALLER]
    init_dependencies(driver, loader)
    get_state()._tenant_allowlist = {_CALLER}
    yield
    reset_capability_validator()


def _register(tenant: str, actions: frozenset[str] = frozenset({"sync:write"})) -> Capability:
    capability = Capability(tenant_id=tenant, domain_id="*", allowed_actions=actions)
    get_capability_validator().register(capability)
    return capability


async def _admin(payload: dict[str, Any]) -> dict[str, Any]:
    from engine.handlers import handle_admin

    return await handle_admin(_CALLER, payload)


async def _revoke(capability_id: str) -> dict[str, Any]:
    return await _admin({"subaction": "revoke_capability", "capability_id": capability_id})


class TestOwnershipPredicate:
    def test_own_capability_is_owned(self) -> None:
        cap = _register(_CALLER)
        assert get_capability_validator().is_owned_by(cap.capability_id, _CALLER)

    def test_other_tenants_capability_is_not_owned(self) -> None:
        cap = _register(_OTHER)
        assert not get_capability_validator().is_owned_by(cap.capability_id, _CALLER)

    def test_delegator_owns_the_child_it_granted(self) -> None:
        validator = get_capability_validator()
        parent = _register(_CALLER, frozenset({"sync:write", "match:read"}))
        child = validator.derive_capability(
            parent, {"allowed_actions": frozenset({"sync:write"})}, target_tenant=_OTHER
        )
        assert validator.is_owned_by(child.capability_id, _CALLER)
        assert validator.is_owned_by(child.capability_id, _OTHER)
        assert not validator.is_owned_by(parent.capability_id, _OTHER)

    def test_wildcard_capability_is_owned_by_no_tenant(self) -> None:
        cap = _register("*")
        validator = get_capability_validator()
        assert not validator.is_owned_by(cap.capability_id, _CALLER)
        assert not validator.is_owned_by(cap.capability_id, "*")

    def test_unknown_id_is_owned_by_no_tenant(self) -> None:
        assert not get_capability_validator().is_owned_by("cap-missing", _CALLER)


class TestRevokeIsTenantBound:
    async def test_cross_tenant_revoke_is_refused_and_changes_nothing(self) -> None:
        from engine.handlers import ValidationError

        foreign = _register(_OTHER)
        with pytest.raises(ValidationError, match=_UNKNOWN_MESSAGE):
            await _revoke(foreign.capability_id)
        assert get_capability_validator()._registry[foreign.capability_id].is_active()

    async def test_unowned_and_unknown_ids_get_the_same_refusal(self) -> None:
        from engine.handlers import ValidationError

        foreign = _register(_OTHER)
        messages = []
        for capability_id in (foreign.capability_id, "cap-missing"):
            with pytest.raises(ValidationError) as caught:
                await _revoke(capability_id)
            messages.append(str(caught.value).replace(capability_id, "<id>"))
        assert messages[0] == messages[1]

    async def test_wildcard_capability_cannot_be_revoked(self) -> None:
        from engine.handlers import ValidationError

        wildcard = _register("*")
        with pytest.raises(ValidationError, match=_UNKNOWN_MESSAGE):
            await _revoke(wildcard.capability_id)
        assert get_capability_validator()._registry[wildcard.capability_id].is_active()

    async def test_owner_revokes_its_capability(self) -> None:
        own = _register(_CALLER)
        result = await _revoke(own.capability_id)
        assert result["status"] == "revoked"
        assert get_capability_validator()._registry[own.capability_id].revoked

    async def test_delegator_revokes_the_child_it_granted(self) -> None:
        validator = get_capability_validator()
        parent = _register(_CALLER, frozenset({"sync:write", "match:read"}))
        child = validator.derive_capability(
            parent, {"allowed_actions": frozenset({"sync:write"})}, target_tenant=_OTHER
        )
        await _revoke(child.capability_id)
        assert validator._registry[child.capability_id].revoked
        assert validator._registry[parent.capability_id].is_active()

    async def test_revoke_response_audit_counts_only_the_callers_capabilities(self) -> None:
        own = _register(_CALLER)
        _register(_OTHER)
        _register(_OTHER)
        result = await _revoke(own.capability_id)
        assert result["audit"]["total_registered"] == 1
        assert result["audit"]["revoked"] == 1


class TestAuditIsTenantScoped:
    async def test_audit_counts_only_owned_capabilities_and_their_derivations(self) -> None:
        validator = get_capability_validator()
        parent = _register(_CALLER, frozenset({"sync:write", "match:read"}))
        validator.derive_capability(parent, {"allowed_actions": frozenset({"sync:write"})}, target_tenant=_OTHER)
        foreign = _register(_OTHER, frozenset({"sync:write", "match:read"}))
        validator.derive_capability(foreign, {"allowed_actions": frozenset({"match:read"})})
        result = await _admin({"subaction": "capability_audit"})
        summary = result["summary"]
        assert summary["total_registered"] == 2  # own parent + the child it delegated
        assert summary["derivations"] == 1
        assert validator.audit_summary()["total_registered"] == 4


class TestFlagOffRestoresLegacy:
    async def test_cross_tenant_revoke_and_registry_wide_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "capability_tenant_binding_enabled", False)
        foreign = _register(_OTHER)
        _register(_CALLER)
        result = await _revoke(foreign.capability_id)
        assert result["status"] == "revoked"
        assert result["audit"]["total_registered"] == 2
        audit = await _admin({"subaction": "capability_audit"})
        assert audit["summary"]["total_registered"] == 2
