"""Runtime pipeline tests for issue #4 (shell injection defence).

These assert two things against runtime/agent.py:

1. ``run_pipeline`` re-validates ticket-derived fields and refuses to run ANY
   subprocess when a malicious base_branch / fix_branch / clone URL is supplied
   (defence in depth).
2. For a valid payload, every recorded subprocess invocation is an argv LIST
   (shell=False) EXCEPT the single auto-detected test-command invocation, which
   is permitted to be a shell string (and never contains ticket data).

SDK stubs for bedrock_agentcore are installed by tests/conftest.py before this
module imports ``agent``. The RecordingRunner fake stands in for subprocess.run.
"""

import pytest

import agent  # noqa: E402 - conftest installs SDK stubs first
from fakes import FakeCompletedProcess, RecordingRunner  # noqa: E402


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


def _payload(**overrides):
    p = dict(VALID_PAYLOAD)
    p.update(overrides)
    return p


INJECTIONS = {
    "base_branch": "main; rm -rf /",
    "fix_branch": "--upload-pack=evil",
    "repo_clone_url": "https://x-token:abc@github.com/o/r.git",
}


@pytest.mark.parametrize("field,bad", list(INJECTIONS.items()))
def test_run_pipeline_rejects_injection_before_any_subprocess(field, bad, monkeypatch):
    """A malicious field must raise before a single subprocess call is made."""
    runner = RecordingRunner()
    monkeypatch.setattr(agent.subprocess, "run", runner)

    with pytest.raises(ValueError):
        agent.run_pipeline(_payload(**{field: bad}))

    assert runner.calls == [], (
        f"a subprocess ran despite invalid {field}={bad!r}: {runner.all_args()}"
    )


def test_run_pipeline_more_injection_branches(monkeypatch):
    for bad in ["../../etc", "a b", "-malicious", "feature..secret"]:
        runner = RecordingRunner()
        monkeypatch.setattr(agent.subprocess, "run", runner)
        with pytest.raises(ValueError):
            agent.run_pipeline(_payload(fix_branch=bad))
        assert runner.calls == [], f"subprocess ran for fix_branch={bad!r}"


def test_valid_payload_uses_argv_lists_except_test_command(monkeypatch, tmp_path):
    """Every recorded command is an argv list except the detected test command."""
    runner = RecordingRunner()
    # Queue results in call order so the pipeline reaches PR creation:
    #  clone, config, config, checkout, status(post-clone), status(post-install),
    #  baseline pytest, kiro-cli, pytest(pass), status(post-fix), git add --,
    #  diff(--quiet rc=1 => changes exist), commit, push, gh pr create.
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # clone
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # config email
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # config name
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # checkout -b
    runner.queue_result(FakeCompletedProcess(None, returncode=0, stdout=""))  # status post-clone
    # Dependency install is skipped (detection returns None for the empty tree).
    runner.queue_result(FakeCompletedProcess(None, returncode=0, stdout=""))  # status post-install
    # A BASELINE pytest run (#9) happens before the first kiro-cli fix.
    runner.queue_result(FakeCompletedProcess(None, returncode=0, stdout="ok"))  # baseline pytest
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # kiro-cli
    runner.queue_result(FakeCompletedProcess(None, returncode=0, stdout="ok"))  # pytest
    # Scoped staging (#13): post-fix status reports one kiro-changed file.
    runner.queue_result(FakeCompletedProcess(None, returncode=0, stdout=" M server.js\n"))  # status post-fix
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # git add -- server.js
    runner.queue_result(FakeCompletedProcess(None, returncode=1))  # diff: changes
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # commit
    runner.queue_result(FakeCompletedProcess(None, returncode=0))  # push
    runner.queue_result(
        FakeCompletedProcess(None, returncode=0,
                             stdout="https://github.com/your-org/your-repo/pull/1")
    )  # gh pr create
    monkeypatch.setattr(agent.subprocess, "run", runner)

    # The detected test command is the ONLY allowed shell string. Make detection
    # deterministic and independent of a real cloned tree. Also stub the install
    # detector so no ecosystem install argv is injected for this assertion.
    monkeypatch.setattr(agent, "_detect_test_command", lambda repo_dir: "pytest")
    monkeypatch.setattr(agent, "_detect_install_command", lambda repo_dir: None)

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert runner.calls, "expected the pipeline to record subprocess calls"

    shell_string_calls = []
    for call in runner.calls:
        args = call["args"]
        if isinstance(args, str):
            shell_string_calls.append(call)
            # A shell string must run with shell=True and be the test command.
            assert call["shell"] is True
            assert args == "pytest"
        else:
            # argv list -> shell=False.
            assert isinstance(args, list)
            assert call["shell"] is False

    # Two shell-string invocations now: the baseline test run (#9) and the
    # post-fix test run. Both are the single detected test command (no ticket
    # data); everything else remains an argv list.
    assert len(shell_string_calls) == 2, (
        f"expected two shell-string calls (baseline + post-fix), got {shell_string_calls}"
    )

    # The git clone invocation is an argv list with the validated branch/url.
    clone_calls = [
        c["args"] for c in runner.calls
        if isinstance(c["args"], list) and c["args"][:2] == ["git", "clone"]
    ]
    assert clone_calls, "expected a git clone argv list"
    assert "master" in clone_calls[0]


def test_run_tests_may_use_shell_string(monkeypatch, tmp_path):
    """step_run_tests uses shell=True for the detected command (no ticket data)."""
    runner = RecordingRunner()
    runner.queue_result(FakeCompletedProcess("npm test", returncode=0, stdout="ok"))
    monkeypatch.setattr(agent.subprocess, "run", runner)
    monkeypatch.setattr(agent, "_detect_test_command", lambda repo_dir: "npm test")

    passed, _output = agent.step_run_tests(tmp_path)

    assert passed is True
    assert len(runner.calls) == 1
    assert runner.calls[0]["args"] == "npm test"
    assert runner.calls[0]["shell"] is True
