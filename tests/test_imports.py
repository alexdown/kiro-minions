"""Smoke test: the SDK stub harness lets the production modules import.

This proves, before any behavior tests arrive, that:

* orchestrator/handler.py imports under the fake boto3 / botocore stubs, and
* runtime/agent.py imports under the fake bedrock_agentcore stubs,

both WITHOUT touching real AWS, GitHub, Jira, or kiro-cli. The stubs are
installed by tests/conftest.py at import time.
"""

import sys


def test_sdk_stubs_are_installed():
    # conftest.install_sdk_stubs() must have replaced the real SDKs.
    assert "boto3" in sys.modules
    assert "botocore.exceptions" in sys.modules
    assert "bedrock_agentcore" in sys.modules
    assert "bedrock_agentcore.runtime" in sys.modules

    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    assert err.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_handler_imports_under_stubs():
    import handler

    # Module-level boto3 resource/client were constructed from the fakes.
    assert hasattr(handler, "handler")
    assert handler._dynamodb is not None
    assert handler._agentcore is not None


def test_agent_imports_under_stubs():
    import agent

    # The BedrockAgentCoreApp fake was constructed and the entrypoint registered.
    assert hasattr(agent, "run_pipeline")
    assert agent.app is not None
    # @app.entrypoint must leave the decorated coroutine usable.
    assert callable(agent.entrypoint)


def test_fake_runner_records_argv_and_shell_flag():
    from fakes import FakeCompletedProcess, RecordingRunner

    runner = RecordingRunner()
    runner.queue_result(FakeCompletedProcess(args=None, returncode=0, stdout="ok"))
    result = runner(["git", "clone", "https://github.com/x/y.git"], shell=False)

    assert result.returncode == 0
    assert runner.calls[0]["args"] == ["git", "clone", "https://github.com/x/y.git"]
    assert runner.calls[0]["shell"] is False
    assert not runner.contains("super-secret-token")
