"""Group F tests (#10, #11, #12): the hardened retry loop.

Covers runtime/agent.py:

* #10: ``step_kiro_fix`` uses a ``KIRO_TIMEOUT_S``-configurable timeout
  (default 300, env-overridable), and a ``subprocess.TimeoutExpired`` raised by
  one kiro attempt is CAUGHT and the loop continues to the next attempt without
  aborting the pipeline.
* #11: the kiro brief is delivered via STDIN (``input=``), NOT argv; the
  kiro-cli argv no longer contains the brief/description text, and test output
  placed in the brief / PR body is truncated to ~3000 chars head+tail including
  the first failing test line.
* #12: ``_fingerprint_failure`` is stable across volatile bits (paths, line
  numbers, timestamps, hex ids) and distinct for genuinely different failures;
  two consecutive identical-fingerprint attempts stop the loop early with
  reason ``repeated_failure`` (mapped to the retryable tests_failed status,
  draft PR); the retry brief includes the previous attempt's (truncated) diff.
* No sentinel secret appears in any recorded argv, stdin, or PR body.

Everything routes through FakeRunner so nothing real runs. The RUNNER now
accepts a backward-compatible ``input=`` kwarg (recorded on each CallRecord).
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from pathlib import Path

import pytest

import agent  # conftest installs SDK stubs first
from fakes import FakeRunner, ScriptedResult


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"
KEY_SENTINEL = "kiro-key-SENTINEL-987654321"


def _payload(**overrides) -> dict:
    p = {
        "repo_clone_url": "https://github.com/your-org/your-repo.git",
        "base_branch": "master",
        "fix_branch": "sonarqube-fix/xss-swig-autoescape",
        "file_to_fix": "server.js:135",
        "description": "Fix XSS: swig.setDefaults({ autoescape: true });",
        "summary": "XSS fix",
        "ticket_id": "PROJ-15",
        "ticket_url": "https://jira.example/browse/PROJ-15",
        "sonar_hash": "6f50f92a-65f2-4c4c-aae5-9b069b44430a",
    }
    p.update(overrides)
    return p


@pytest.fixture(autouse=True)
def restore_runner():
    original = agent.RUNNER
    yield
    agent.RUNNER = original


def _make_workdir(monkeypatch, *, markers=("tests",)) -> Path:
    """Create a fake temp workdir whose repo_dir has the given marker dirs/files."""
    base = Path(tempfile.mkdtemp(prefix="kmtest-"))
    workdir = base / "work"
    workdir.mkdir()
    repo_dir = workdir / "your-repo"
    repo_dir.mkdir(parents=True)
    for marker in markers:
        target = repo_dir / marker
        if "." in marker:
            target.write_text("")
        else:
            target.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _fake_tempdir(*args, **kwargs):
        yield str(workdir)

    monkeypatch.setattr(agent.tempfile, "TemporaryDirectory", _fake_tempdir)
    return workdir


# ---------------------------------------------------------------------------
# #10: configurable timeout
# ---------------------------------------------------------------------------


def test_kiro_fix_uses_default_timeout_constant():
    """step_kiro_fix records timeout == KIRO_TIMEOUT_S (default 300)."""
    runner = FakeRunner()
    agent.RUNNER = runner

    agent.step_kiro_fix(Path("/tmp/repo"), "do the thing", None, 1, None)

    kiro = runner.find("kiro-cli chat")
    assert kiro is not None
    assert kiro.timeout == agent.KIRO_TIMEOUT_S == 300


def test_kiro_timeout_is_env_configurable(monkeypatch):
    """KIRO_TIMEOUT_S overrides the per-attempt timeout after a module reload."""
    import importlib

    monkeypatch.setenv("KIRO_TIMEOUT_S", "42")
    reloaded = importlib.reload(agent)
    try:
        assert reloaded.KIRO_TIMEOUT_S == 42
        runner = FakeRunner()
        reloaded.RUNNER = runner
        reloaded.step_kiro_fix(Path("/tmp/repo"), "x", None, 1, None)
        kiro = runner.find("kiro-cli chat")
        assert kiro.timeout == 42
    finally:
        # Restore the module to the default-env state for the rest of the suite.
        monkeypatch.delenv("KIRO_TIMEOUT_S", raising=False)
        importlib.reload(agent)


# ---------------------------------------------------------------------------
# #11: brief via stdin, not argv
# ---------------------------------------------------------------------------


def test_brief_delivered_via_stdin_not_argv():
    """The brief text is NOT in the kiro-cli argv; it is fed via input= (stdin)."""
    runner = FakeRunner()
    agent.RUNNER = runner

    description = "UNIQUE_BRIEF_MARKER fix the autoescape issue"
    agent.step_kiro_fix(Path("/tmp/repo"), description, "server.js:135", 1, None)

    kiro = runner.find("kiro-cli chat")
    assert isinstance(kiro.cmd, list)
    # The first four argv elements are preserved; the brief is NOT appended.
    assert kiro.cmd == ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"]
    assert all("UNIQUE_BRIEF_MARKER" not in str(part) for part in kiro.cmd)
    # The brief is delivered via stdin and contains the description + file hint.
    assert kiro.input is not None
    assert "UNIQUE_BRIEF_MARKER" in kiro.input
    assert "server.js:135" in kiro.input


# ---------------------------------------------------------------------------
# #11: truncation (reuses the FEAT-002 helper)
# ---------------------------------------------------------------------------


def test_truncation_keeps_budget_and_first_failing_line():
    budget = agent.TEST_OUTPUT_BUDGET
    first_fail = "FAILED tests/test_core.py::test_important AssertionError"
    # A long body whose first failing line sits near the top (would be dropped
    # once head is halved), followed by filler, then a tail.
    body = (
        first_fail + "\n"
        + ("filler line padding to force truncation\n" * 400)
        + "TAIL_MARKER end of the log\n"
    )
    out = agent.truncate_test_output(body)

    assert len(body) > budget  # precondition: truncation actually happens
    # Budget is approximate (marker + surfaced first-failure block add a little).
    assert len(out) <= budget + 200
    assert "...[truncated]..." in out
    assert "TAIL_MARKER" in out
    # The first failing test line survives truncation (surfaced explicitly).
    assert "test_important" in out


# ---------------------------------------------------------------------------
# #12: fingerprinting
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_volatile_bits():
    """Same logical failure at different paths/lines/timestamps -> same hash."""
    out_a = (
        "2024-01-01 10:00:00 running\n"
        "FAILED /home/alice/proj/tests/test_x.py::test_a\n"
        "AssertionError at /home/alice/proj/src/mod.py:135 object 0xdeadbeef\n"
        "1 failed in 2.31s\n"
    )
    out_b = (
        "2025-06-07 23:59:59 running\n"
        "FAILED /var/ci/build/tests/test_x.py::test_a\n"
        "AssertionError at /var/ci/build/src/mod.py:999 object 0xcafef00d\n"
        "1 failed in 7.02s\n"
    )
    assert agent._fingerprint_failure(out_a) == agent._fingerprint_failure(out_b)


def test_fingerprint_differs_for_different_failures():
    out_a = "FAILED tests/test_x.py::test_a\nAssertionError: expected 1"
    out_b = "FAILED tests/test_y.py::test_b\nValueError: boom"
    assert agent._fingerprint_failure(out_a) != agent._fingerprint_failure(out_b)


def test_fingerprint_empty_output():
    assert agent._fingerprint_failure("") == ""


# Review finding #2: numeric-only differences must NOT collapse to the same
# fingerprint (previously `_NUMBER_RE` stripped every bare integer, so
# `expected 1` and `expected 2` hashed identically and triggered a FALSE
# repeated_failure early-stop).


def test_fingerprint_differs_for_meaningful_numeric_value():
    """Same test id, assertion differs ONLY by a numeric value -> DIFFERENT hash."""
    out_a = "FAILED tests/test_x.py::test_a\nAssertionError: expected 1 but got 0"
    out_b = "FAILED tests/test_x.py::test_a\nAssertionError: expected 2 but got 0"
    assert agent._fingerprint_failure(out_a) != agent._fingerprint_failure(out_b)


def test_fingerprint_still_stable_for_volatile_numbers():
    """Volatile numbers (line/col, durations, memory ids) still normalize away."""
    out_a = (
        "FAILED tests/test_x.py::test_a\n"
        "AssertionError at line 135 object at 140234567890\n"
        "1 failed in 2.31s\n"
    )
    out_b = (
        "FAILED tests/test_x.py::test_a\n"
        "AssertionError at line 999 object at 140999999999\n"
        "1 failed in 7.02s\n"
    )
    assert agent._fingerprint_failure(out_a) == agent._fingerprint_failure(out_b)


def test_normalize_error_line_keeps_assertion_value():
    """`expected 2` survives normalization; `line N` / durations do not."""
    norm1 = agent._normalize_error_line("AssertionError: expected 1 at line 42")
    norm2 = agent._normalize_error_line("AssertionError: expected 2 at line 99")
    # The meaningful value differs...
    assert norm1 != norm2
    assert "expected 1" in norm1
    assert "expected 2" in norm2
    # ...but the volatile "line N" marker is normalized away identically.
    assert "line 42" not in norm1
    assert "line 99" not in norm2


# ---------------------------------------------------------------------------
# #10: TimeoutExpired is caught per attempt and the loop continues
# ---------------------------------------------------------------------------


def test_timeout_on_first_attempt_is_caught_and_loop_continues(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    timeout_exc = subprocess.TimeoutExpired(cmd="kiro-cli", timeout=300)
    runner = FakeRunner(
        results=[
            # Baseline pytest (red) so a retry is meaningful.
            ScriptedResult(returncode=1, stdout="FAILED tests/test_x.py::test_a",
                           match="pytest"),
            # First kiro attempt times out.
            ScriptedResult(raises=timeout_exc, match="kiro-cli chat"),
            # Second kiro attempt succeeds, tests then pass.
            ScriptedResult(returncode=0, stdout="all passed", match="pytest"),
            # Scoped staging (#13): three porcelain snapshots; the post-fix one
            # reports a kiro-changed file so there is something to commit.
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/9", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    # Does NOT raise: the timeout is caught and the loop continues.
    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    # kiro-cli ran twice: the timed-out attempt and the succeeding retry.
    assert runner.count("kiro-cli chat") == 2
    assert result["attempts"] == 2


# ---------------------------------------------------------------------------
# #12: repeated_failure early stop + previous diff in brief
# ---------------------------------------------------------------------------


def test_repeated_failure_stops_early_and_opens_draft(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    same_fail = "FAILED tests/test_x.py::test_a\nAssertionError: still broken"
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=0, stdout="baseline ok", match="pytest"),
            # Attempt 1 and attempt 2 produce the IDENTICAL failure -> stop early.
            ScriptedResult(returncode=1, stdout=same_fail, match="pytest"),
            ScriptedResult(returncode=1, stdout=same_fail, match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/10", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["repeated_failure"] is True
    assert "repeated_failure" in result["reason"]
    assert result["draft"] is True
    # Stopped at attempt 2, BEFORE exhausting MAX_FIX_ATTEMPTS (3).
    assert result["attempts"] == 2 < agent.MAX_FIX_ATTEMPTS
    # kiro ran only twice (not three times).
    assert runner.count("kiro-cli chat") == 2

    # The draft PR body notes the early stop.
    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "repeated_failure" in body


# ---------------------------------------------------------------------------
# Review finding #1: deps are re-installed after a retry reset
# ---------------------------------------------------------------------------


def test_deps_reinstalled_after_retry_reset(monkeypatch):
    """`git clean -fd` on retry can wipe non-gitignored deps; install re-runs.

    The repo has a requirements.txt so `_detect_install_command` yields
    ['pip','install','-r','requirements.txt']. Attempt 1 fails (distinct
    fingerprint), the tree is reset, and the dependency install must run AGAIN
    before attempt 2's test run so the retry is deterministic regardless of the
    repo's ignore rules.
    """
    # requirements.txt (install marker) + tests/ (test-runner marker).
    _make_workdir(monkeypatch, markers=("requirements.txt", "tests"))
    runner = FakeRunner(
        results=[
            # Baseline pytest (red) so a retry is meaningful.
            ScriptedResult(returncode=1, stdout="FAILED tests/test_x.py::test_a",
                           match="pytest"),
            # Attempt 1 fails with a DISTINCT fingerprint (no early stop).
            ScriptedResult(returncode=1, stdout="FAILED tests/test_y.py::test_b",
                           match="pytest"),
            # Attempt 2 passes.
            ScriptedResult(returncode=0, stdout="all passed", match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/7", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())
    assert result["status"] == "success"
    assert result["attempts"] == 2

    # The install command ran at least twice: once before the loop (#9) and
    # once after the retry reset (#1) so attempt 2 has a provisioned tree.
    install_calls = runner.count("pip install -r requirements.txt")
    assert install_calls >= 2, (
        f"expected dependency install to re-run after reset, saw "
        f"{install_calls} install call(s)"
    )
    # The reset itself still happened (checkout + clean) before the re-install.
    assert runner.count("git clean -fd") >= 1


def test_deps_reinstalled_after_timeout_reset(monkeypatch):
    """The timeout retry path also re-installs deps after resetting the tree."""
    _make_workdir(monkeypatch, markers=("requirements.txt", "tests"))
    timeout_exc = subprocess.TimeoutExpired(cmd="kiro-cli", timeout=300)
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=1, stdout="FAILED tests/test_x.py::test_a",
                           match="pytest"),
            # Attempt 1 kiro times out -> reset + re-install, then continue.
            ScriptedResult(raises=timeout_exc, match="kiro-cli chat"),
            # Attempt 2 passes.
            ScriptedResult(returncode=0, stdout="all passed", match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/11", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())
    assert result["status"] == "success"
    assert result["attempts"] == 2
    # Install before the loop + install after the timeout reset.
    assert runner.count("pip install -r requirements.txt") >= 2


def test_retry_brief_includes_previous_diff(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    diff_text = "diff --git a/server.js b/server.js\n+autoescape: true"
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=0, stdout="baseline ok", match="pytest"),
            # Attempt 1 fails with a DISTINCT fingerprint so we retry (no early stop).
            ScriptedResult(returncode=1, stdout="FAILED tests/test_x.py::test_a",
                           match="pytest"),
            # git diff (captured before reset) returns the prior attempt's diff.
            ScriptedResult(stdout=diff_text, match="git diff"),
            # Attempt 2 passes.
            ScriptedResult(returncode=0, stdout="all passed", match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/8", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())
    assert result["status"] == "success"

    # The SECOND kiro invocation's stdin brief includes the previous diff text.
    kiro_calls = [c for c in runner.calls if "kiro-cli chat" in c.display]
    assert len(kiro_calls) == 2
    second_brief = kiro_calls[1].input or ""
    assert "autoescape: true" in second_brief
    # And the previous failing output is referenced too.
    assert "PREVIOUS ATTEMPT FAILED" in second_brief


# ---------------------------------------------------------------------------
# secret hygiene across the retry loop
# ---------------------------------------------------------------------------


def test_no_secret_in_argv_stdin_or_pr_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    _make_workdir(monkeypatch, markers=("tests",))
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=1, stdout="FAILED tests/test_x.py::test_a",
                           match="pytest"),
            ScriptedResult(returncode=0, stdout="all passed", match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/5", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    # Put both sentinels into the description (which flows into the kiro brief
    # via stdin); redact() must mask them so neither surfaces in argv, stdin, or
    # the PR body.
    result = agent.run_pipeline(_payload(
        description=f"fix with {TOKEN_SENTINEL} and {KEY_SENTINEL}",
    ))
    assert result["status"] == "success"

    for call in runner.calls:
        parts = call.cmd if isinstance(call.cmd, list) else [call.cmd]
        blob = "\n".join(str(p) for p in parts)
        if call.input:
            blob += "\n" + call.input
        assert TOKEN_SENTINEL not in blob
        assert KEY_SENTINEL not in blob
