"""Pytest configuration and shared fixtures for the kiro-minions test suite.

The production modules (orchestrator/handler.py, runtime/agent.py) import boto3
and bedrock_agentcore at module top level. Those packages are not installable on
the sandbox interpreter (read-only global site-packages), so we stub them into
``sys.modules`` BEFORE any test imports the production modules.
``install_sdk_stubs()`` runs here at import time (module scope), which pytest
evaluates before collecting/importing test modules. This is why the step-1
handler fixtures below are wired to the in-memory fakes rather than moto: moto +
real boto3 cannot be installed here, and the SDK stubs make the characterization
tests runnable with zero installs.

We also extend ``sys.path`` with the orchestrator/ and runtime/ directories,
mirroring the ``sys.path.insert`` pattern already used in tests/test_parser.py,
so ``import handler`` and ``import agent`` resolve without packaging.
"""

import importlib
import os
import sys

import pytest

# Make the fakes importable whether pytest is run from the repo root or tests/.
_THIS_DIR = os.path.dirname(__file__)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from fakes import (  # noqa: E402
    FakeAgentCoreClient,
    FakeDynamoResource,
    FakeSqsClient,
    install_sdk_stubs,
)

# Install SDK stubs at import time — before any test module imports handler/agent.
install_sdk_stubs()

# Extend sys.path so orchestrator/ and runtime/ modules import directly.
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for _subdir in ("orchestrator", "runtime"):
    _path = os.path.join(_REPO_ROOT, _subdir)
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --- AWS/runtime constants (source of truth: deploy/template.yaml) ----------
TABLE_NAME = "kiro-minions-jobs"
AWS_REGION = "us-east-1"
DUMMY_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/kiro-minions-test"
)
DUMMY_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/kiro-minions-work"


# --- step-2 fixtures: fresh service fakes ----------------------------------


@pytest.fixture
def fake_dynamo():
    """A fresh FakeDynamoResource for a single test."""
    return FakeDynamoResource()


@pytest.fixture
def fake_sqs():
    """A fresh FakeSqsClient for a single test."""
    return FakeSqsClient()


@pytest.fixture
def fake_agentcore():
    """A fresh FakeAgentCoreClient for a single test."""
    return FakeAgentCoreClient()


@pytest.fixture(autouse=True)
def _reset_sdk_stubs():
    """Re-install clean SDK stubs around every test for isolation.

    Guarantees a test that swaps a module-level client/resource cannot leak
    state into the next test.
    """
    install_sdk_stubs()
    yield
    install_sdk_stubs()


@pytest.fixture(autouse=True)
def _gates_off_by_default(monkeypatch):
    """Default both verification gates OFF for the suite.

    The gates default ON in production (opt-out). The legacy pipeline
    characterization tests (test_agent_phase/test_agent_argv/
    test_secret_redaction) script the exact subprocess sequence of the
    pre-gate pipeline, so they must run with gates OFF. Gate-specific tests
    (test_review_gates/test_spec_gates/test_oneshot_gates) set these env vars
    themselves and reload review_gates, overriding this default.
    """
    monkeypatch.setenv("SPEC_REVIEW_ENABLED", "0")
    monkeypatch.setenv("SPEC_VERIFY_SPEC_ENABLED", "0")
    try:
        import review_gates  # noqa: E402 - sys.path set above
        importlib.reload(review_gates)
    except Exception:
        # review_gates imports agent (SDK stubs) — if unavailable in a given
        # collection context, the gate tests that need it manage their own
        # reload; nothing else depends on it.
        pass


# --- step-1 characterization fixtures (adapted to the SDK stubs) -----------


@pytest.fixture
def aws_env(monkeypatch):
    """Set AWS region + dummy credentials + runtime/queue env for the handler."""
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AGENTCORE_RUNTIME_ARN", DUMMY_RUNTIME_ARN)
    monkeypatch.setenv("DYNAMODB_TABLE", TABLE_NAME)
    monkeypatch.setenv("SQS_QUEUE_URL", DUMMY_QUEUE_URL)
    return None


@pytest.fixture
def handler(aws_env):
    """Yield orchestrator.handler reloaded against fresh in-memory fakes.

    handler.py builds its module-level ``_dynamodb`` / ``_agentcore`` / ``_sqs``
    at import time, so we reinstall clean SDK stubs and reload the module to get
    fresh fake instances. The fake DynamoDB resource persists items across the
    ``.Table(name)`` calls handler.py makes within a request, so ``jobs_table``
    can read back what the handler wrote.
    """
    install_sdk_stubs()
    import handler as handler_module

    handler_module = importlib.reload(handler_module)
    yield handler_module


@pytest.fixture
def jobs_table(handler):
    """The FakeDynamoTable the reloaded handler writes to."""
    return handler._dynamodb.Table(TABLE_NAME)


class FakeAgentCore(FakeAgentCoreClient):
    """Stub for handler._agentcore (step-1 naming).

    Thin alias over FakeAgentCoreClient so ``.calls`` records each ``invoke``
    (runtimeArn / runtimeSessionId / payload) without touching real AWS.
    """


@pytest.fixture
def agentcore_stub(handler):
    """Replace handler._agentcore with a recording stub and return it."""
    stub = FakeAgentCore()
    handler._agentcore = stub
    return stub
