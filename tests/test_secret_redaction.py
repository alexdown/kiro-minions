"""Tests for issue #3: secrets must never leak into argv, logs, exceptions,
or HTTP response bodies.

Covers:
  * runtime/agent.py ``redact()`` directly (token present/empty/None/multi).
  * A failing command through ``agent._run``: the raised exception AND the
    captured log records are redacted and never contain the sentinel.
  * The clone step / pipeline: the sentinel token never appears in any recorded
    argv, and git auth is configured via ``gh auth setup-git`` without the token
    in argv; the clone URL argv is the plain https URL.
  * orchestrator/handler.py ``redact()`` and the response bodies: no sentinel
    leaks into any returned error string, and the raw runtime stream text is not
    echoed back to the caller.

SDK stubs for boto3/bedrock_agentcore are installed by tests/conftest.py before
these imports resolve. The RecordingRunner fake stands in for subprocess.run.
"""

import logging

import pytest

import agent  # noqa: E402 - conftest installs SDK stubs first
import handler  # noqa: E402
from fakes import FakeCompletedProcess, RecordingRunner  # noqa: E402


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"
KEY_SENTINEL = "kiro-key-SENTINEL-987654321"


# ---------------------------------------------------------------------------
# redact() unit tests (runtime side)
# ---------------------------------------------------------------------------


def test_redact_masks_token_when_present(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    out = agent.redact(f"cloning with {TOKEN_SENTINEL} now")
    assert TOKEN_SENTINEL not in out
    assert agent.REDACTION_MASK in out


def test_redact_no_masking_when_token_empty(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    # An empty env value must never cause the empty string to be masked.
    text = "nothing secret here"
    assert agent.redact(text) == text
    assert agent.REDACTION_MASK not in agent.redact(text)


def test_redact_no_masking_when_token_unset(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    text = "plain text"
    assert agent.redact(text) == text


def test_redact_masks_multiple_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    out = agent.redact(f"token={TOKEN_SENTINEL} key={KEY_SENTINEL}")
    assert TOKEN_SENTINEL not in out
    assert KEY_SENTINEL not in out
    assert out.count(agent.REDACTION_MASK) == 2


def test_redact_none_input(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    assert agent.redact(None) is None


# ---------------------------------------------------------------------------
# _run: failing command must redact exception + logs
# ---------------------------------------------------------------------------


def test_run_failure_redacts_exception_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)

    runner = RecordingRunner()
    # A non-zero result whose stderr/stdout carry both sentinels.
    runner.queue_result(FakeCompletedProcess(
        None,
        returncode=1,
        stdout=f"output mentioning {TOKEN_SENTINEL}",
        stderr=f"fatal: auth failed for {TOKEN_SENTINEL} / {KEY_SENTINEL}",
    ))
    monkeypatch.setattr(agent.subprocess, "run", runner)

    caplog.set_level(logging.INFO, logger="kiro-minions-agent")

    with pytest.raises(RuntimeError) as excinfo:
        agent._run(["git", "clone", "https://github.com/o/r.git", "/tmp/x"])

    msg = str(excinfo.value)
    assert TOKEN_SENTINEL not in msg
    assert KEY_SENTINEL not in msg
    assert agent.REDACTION_MASK in msg

    all_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN_SENTINEL not in all_logs
    assert KEY_SENTINEL not in all_logs
    # The captured STDOUT/STDERR logs were masked.
    assert agent.REDACTION_MASK in all_logs


# ---------------------------------------------------------------------------
# clone step / pipeline: no token in argv, auth via gh
# ---------------------------------------------------------------------------


VALID_PAYLOAD = {
    "ticket_id": "PROJ-15",
    "summary": "XSS fix",
    "description": "Enable autoescape",
    "ticket_url": "https://jira.example.com/browse/PROJ-15",
    "repo_clone_url": "https://github.com/your-org/your-repo.git",
    "base_branch": "master",
    "fix_branch": "sonarqube-fix/xss-swig-autoescape",
    "file_to_fix": "server.js:135",
    "sonar_hash": "6f50f92a-65f2-4c4c-aae5-9b069b44430a",
}


def _full_pipeline_runner() -> RecordingRunner:
    """A runner queued so run_pipeline reaches PR creation successfully."""
    runner = RecordingRunner()
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # gh auth setup-git
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # clone
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # config email
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # config name
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # checkout -b
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # kiro-cli
    runner.queue_result(FakeCompletedProcess(None, returncode=0, stdout="ok"))  # pytest
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # git add -A
    runner.queue_result(FakeCompletedProcess(None, returncode=1))  # diff: changes
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # commit
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # push
    runner.queue_result(FakeCompletedProcess(
        None, returncode=0,
        stdout="https://github.com/your-org/your-repo/pull/1"))  # gh pr create
    return runner


def test_pipeline_never_puts_token_in_argv(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)

    runner = _full_pipeline_runner()
    monkeypatch.setattr(agent.subprocess, "run", runner)
    monkeypatch.setattr(agent, "_detect_test_command", lambda repo_dir: "pytest")

    result = agent.run_pipeline(dict(VALID_PAYLOAD))
    assert result["status"] == "success"

    # The token / key must not appear in ANY recorded argv or shell string.
    assert runner.contains(TOKEN_SENTINEL) is False
    assert runner.contains(KEY_SENTINEL) is False

    # The clone URL argv is the plain https URL (no x-token:<token>@ prefix).
    clone_calls = [
        c["args"] for c in runner.calls
        if isinstance(c["args"], list) and c["args"][:2] == ["git", "clone"]
    ]
    assert clone_calls, "expected a git clone argv list"
    assert "https://github.com/your-org/your-repo.git" in clone_calls[0]
    assert all("x-token" not in str(a) for a in clone_calls[0])


def test_pipeline_configures_gh_auth_without_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)

    runner = _full_pipeline_runner()
    monkeypatch.setattr(agent.subprocess, "run", runner)
    monkeypatch.setattr(agent, "_detect_test_command", lambda repo_dir: "pytest")

    agent.run_pipeline(dict(VALID_PAYLOAD))

    gh_auth_calls = [
        c["args"] for c in runner.calls
        if isinstance(c["args"], list) and c["args"][:2] == ["gh", "auth"]
    ]
    assert gh_auth_calls == [["gh", "auth", "setup-git"]], (
        f"expected a single `gh auth setup-git` argv, got {gh_auth_calls}"
    )
    # The token is never an argument to the gh auth command.
    assert all(TOKEN_SENTINEL not in str(a) for a in gh_auth_calls[0])


def test_setup_git_auth_skipped_when_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    runner = RecordingRunner()
    monkeypatch.setattr(agent.subprocess, "run", runner)

    agent.setup_git_auth()

    # No gh auth call when there is no token to authenticate with.
    assert runner.calls == []


# ---------------------------------------------------------------------------
# orchestrator handler: redaction + no raw stream echo
# ---------------------------------------------------------------------------


def test_handler_redact_masks_env_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    out = handler.redact(f"boom {TOKEN_SENTINEL}")
    assert TOKEN_SENTINEL not in out
    assert handler.REDACTION_MASK in out


def test_handler_redact_none_and_empty(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "")
    assert handler.redact(None) is None
    assert handler.redact("plain") == "plain"


def _webhook_event(description: str) -> dict:
    return {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "XSS fix",
                "description": description,
                "labels": [
                    "sonarqube",
                    "sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a",
                ],
            },
        }
    }


VALID_DESCRIPTION = (
    "Clone: https://github.com/your-org/your-repo.git\n"
    "Base branch: master\n"
    "Suggested fix branch: sonarqube-fix/xss-swig-autoescape\n"
    "File to fix: server.js:135\n"
)


def test_handler_dispatch_does_not_echo_runtime_stream(monkeypatch):
    """A successful dispatch must not return raw runtime stream text."""
    import json as _json

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setattr(handler, "AGENTCORE_RUNTIME_ARN", "arn:aws:fake")
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)

    # The runtime 'stream' echoes the sentinel; the handler must NOT return it.
    def fake_invoke(payload):
        return f"runtime log leaked {TOKEN_SENTINEL}"

    monkeypatch.setattr(handler, "_invoke_runtime", fake_invoke)

    resp = handler.handler(_webhook_event(VALID_DESCRIPTION), None)
    assert resp["statusCode"] == 200
    body = _json.loads(resp["body"])
    assert body["status"] == "dispatched"
    assert "runtime_response" not in body
    assert TOKEN_SENTINEL not in resp["body"]


def test_handler_runtime_failure_error_is_redacted(monkeypatch):
    import json as _json

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setattr(handler, "AGENTCORE_RUNTIME_ARN", "arn:aws:fake")
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)
    monkeypatch.setattr(handler, "_mark_status", lambda *a, **k: None)

    def boom(payload):
        raise RuntimeError(f"invoke failed: credential {TOKEN_SENTINEL} rejected")

    monkeypatch.setattr(handler, "_invoke_runtime", boom)

    resp = handler.handler(_webhook_event(VALID_DESCRIPTION), None)
    assert resp["statusCode"] == 500
    assert TOKEN_SENTINEL not in resp["body"]
    assert handler.REDACTION_MASK in _json.loads(resp["body"])["error"]
