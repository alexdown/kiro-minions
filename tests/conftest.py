"""Shared pytest fixtures and import shims for kiro-minions tests.

Two jobs here:

1.  Install a ``sys.modules`` shim for ``bedrock_agentcore`` /
    ``bedrock_agentcore.runtime`` BEFORE any test imports ``runtime/agent.py``.
    The real package is intentionally NOT installed, yet agent.py does
    ``from bedrock_agentcore.runtime import BedrockAgentCoreApp`` at module top.
    The shim provides a dummy ``BedrockAgentCoreApp`` whose ``entrypoint`` is a
    pass-through decorator and whose ``run`` is a no-op.

2.  Provide moto-backed fixtures for ``orchestrator/handler.py``. handler.py
    creates its boto3 resource/client at IMPORT time, so the moto mock must be
    active BEFORE the module is (re)loaded -- the ``handler`` fixture starts
    ``mock_aws``, creates the DynamoDB table matching deploy/template.yaml, then
    reloads the module so ``_dynamodb`` is recreated under the mock.
"""

from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

# --- make orchestrator/ and runtime/ importable (mirrors test_parser.py) ----
_HERE = os.path.dirname(__file__)
for _rel in ("..", "../orchestrator", "../runtime"):
    _path = os.path.join(_HERE, _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --- bedrock_agentcore sys.modules shim (installed at import time) ----------
def _install_bedrock_agentcore_shim() -> None:
    if "bedrock_agentcore" in sys.modules:
        return

    class _DummyApp:
        """Stand-in for BedrockAgentCoreApp used only so import succeeds."""

        def entrypoint(self, fn):  # pass-through decorator
            return fn

        def run(self, *args, **kwargs):  # no-op server start
            return None

    pkg = types.ModuleType("bedrock_agentcore")
    runtime_mod = types.ModuleType("bedrock_agentcore.runtime")
    runtime_mod.BedrockAgentCoreApp = _DummyApp
    pkg.runtime = runtime_mod

    sys.modules["bedrock_agentcore"] = pkg
    sys.modules["bedrock_agentcore.runtime"] = runtime_mod


_install_bedrock_agentcore_shim()


# --- AWS/DynamoDB constants (source of truth: deploy/template.yaml) ---------
TABLE_NAME = "kiro-minions-jobs"
AWS_REGION = "us-east-1"
DUMMY_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/kiro-minions-test"
)


@pytest.fixture
def aws_env(monkeypatch):
    """Set AWS region + dummy credentials in the environment."""
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AGENTCORE_RUNTIME_ARN", DUMMY_RUNTIME_ARN)
    monkeypatch.setenv("DYNAMODB_TABLE", TABLE_NAME)
    return None


@pytest.fixture
def handler(aws_env):
    """Yield orchestrator.handler reloaded under an active moto mock.

    A DynamoDB table matching deploy/template.yaml is created inside the mock
    first, then the module is reloaded so its module-level ``_dynamodb`` /
    ``_agentcore`` are recreated against the mocked AWS.
    """
    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
        ddb.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "sonar_hash", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "sonar_hash", "KeyType": "HASH"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.Table(TABLE_NAME).wait_until_exists()

        import handler as handler_module

        handler_module = importlib.reload(handler_module)
        yield handler_module


@pytest.fixture
def jobs_table(handler):
    """DynamoDB Table resource for the mocked jobs table."""
    import boto3

    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


class FakeAgentCore:
    """Stub for handler._agentcore.

    Records each ``invoke`` call and returns a canned body response so
    ``_invoke_runtime`` aggregates text without touching real AWS.
    """

    def __init__(self, response=None):
        self.calls: list[dict] = []
        self._response = response or {"body": b'{"status": "ok"}'}

    def invoke(self, runtimeArn=None, runtimeSessionId=None, payload=None, **kwargs):
        self.calls.append(
            {
                "runtimeArn": runtimeArn,
                "runtimeSessionId": runtimeSessionId,
                "payload": payload,
                **kwargs,
            }
        )
        return self._response


@pytest.fixture
def agentcore_stub(handler):
    """Replace handler._agentcore with a recording stub and return it."""
    stub = FakeAgentCore()
    handler._agentcore = stub
    return stub
