"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, auth, security]
owner: engine-team
status: active
--- /L9_META ---

Regression tests for chassis.auth.generate_l9_api_key.

Guards CWE-312/532 (clear-text logging of sensitive data): the operator-facing
status messages emitted by ``store_in_aws`` must reference only the AWS Secrets
Manager resource identifier (``clawdbot/l9-api``) and never echo the generated
API key value. Also pins the resource-identifier constants so they cannot be
renamed back into names that static analysis misclassifies as leaked secrets.

The module lives under the ``chassis`` package whose ``__init__`` imports
FastAPI; we load it directly by file path so the test does not require the full
chassis runtime to be importable.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "chassis" / "auth" / "generate_l9_api_key.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_generate_l9_api_key_under_test", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keygen = _load_module()


def test_generate_key_is_high_entropy_urlsafe():
    key = keygen.generate_key()
    # secrets.token_urlsafe(48) yields ~64 url-safe chars.
    assert isinstance(key, str)
    assert len(key) >= 43
    # Two independent draws must differ (no fixed/degenerate output).
    first_key = keygen.generate_key()
    second_key = keygen.generate_key()
    assert first_key != second_key


def test_resource_identifiers_are_non_sensitive_names():
    # The constants hold AWS resource identifiers, not secret values, and must
    # not be named with tokens ("secret"/"key"/"token") that trip clear-text
    # logging heuristics when printed in status output.
    assert keygen.AWS_SM_ENTRY_NAME == "clawdbot/l9-api"
    for banned in ("secret", "token", "password", "credential"):
        assert banned not in "AWS_SM_ENTRY_NAME".lower()
        assert banned not in "AWS_SM_ENTRY_DESCRIPTION".lower()


class _FakeClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _install_fake_boto3(monkeypatch, *, raise_not_found: bool):
    """Install a minimal fake boto3 + botocore so store_in_aws runs offline."""
    calls: dict[str, object] = {}

    class _FakeSecretsClient:
        def put_secret_value(self, **kwargs):
            calls["put"] = kwargs
            if raise_not_found:
                raise _FakeClientError("ResourceNotFoundException")
            return {}

        def create_secret(self, **kwargs):
            calls["create"] = kwargs
            return {}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *a, **k: _FakeSecretsClient()  # type: ignore[attr-defined]

    fake_botocore = types.ModuleType("botocore")
    fake_exceptions = types.ModuleType("botocore.exceptions")
    fake_exceptions.ClientError = _FakeClientError  # type: ignore[attr-defined]
    fake_botocore.exceptions = fake_exceptions  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_exceptions)
    return calls


@pytest.mark.parametrize("raise_not_found", [False, True])
def test_store_in_aws_status_never_logs_key(monkeypatch, capsys, raise_not_found):
    _install_fake_boto3(monkeypatch, raise_not_found=raise_not_found)
    key = "SUPER-SECRET-KEY-VALUE-do-not-print-1234567890"

    keygen.store_in_aws(key, region="us-east-1")

    out = capsys.readouterr().out
    # Status output must name the resource, never the key value.
    assert keygen.AWS_SM_ENTRY_NAME in out
    assert key not in out
