"""FEAT-003 tests: the one-shot minion's POST-FIX verification gates (ADR-008 Q6).

These exercise the gate phase wired into ``runtime/agent.run_pipeline``. ADR-008
Q6 was OVERRIDDEN by the maintainer to apply the shared gates to BOTH worker
paths, so the one-shot path gains the SAME ``review_gates.run_gates`` call the
spec-executor uses — as a POST-FIX phase that runs ONLY on the SUCCESS path
(fix loop produced ``tests_passed=True``, ``step_commit`` found changes, and
``step_push`` ran), BEFORE the otherwise-non-draft ``step_open_pr``.

On the one-shot path:
  * Gate A (adversarial review) runs on the cumulative ``base..fix`` diff with
    the ticket description/summary as intent;
  * Gate B (verify-vs-spec) is a documented SKIP-WITH-NOTICE (the one-shot path
    has no Kiro spec, so ``acceptance_text=None`` -> Q3 absent-spec rule).

Flags default ON (opt-out); with both flags opted OFF ``run_pipeline`` is
byte-for-byte unchanged (no ``git diff base..fix``, no extra kiro-cli review
call, non-draft PR, no ``## Verification`` section). The existing one-shot tests
(tests/test_agent.py etc.) cover that OFF behaviour unmodified; a dedicated
flags-OFF regression lives here too.

Flags-ON tests monkeypatch the env and ``importlib.reload(review_gates)`` so the
module-level gate flags apply (agent does NOT own the flags, so it is NOT
reloaded). Teardown reloads review_gates back to flags-OFF so other modules are
unaffected.

Harness note (FEAT-001 findings): the ScriptedResult-based FakeRunner has NO
``contains()`` / ``flat_text()`` (those are on RecordingRunner); we iterate
``runner.calls`` and inspect ``CallRecord.cmd`` / ``.input``. The cumulative
diff is scripted via ``ScriptedResult(match='git diff master..')`` and the
Gate A verdict via ``ScriptedResult(match='kiro-cli chat')`` (ordered FIFO among
the kiro matchers: the single coding pass first, then the review verdict).
"""

from __future__ import annotations

import contextlib
import importlib
import tempfile
from pathlib import Path

import pytest

import agent  # conftest installs SDK stubs first
import review_gates
from fakes import FakeRunner, ScriptedResult


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"
KEY_SENTINEL = "kiro-key-SENTINEL-987654321"

DIFF_TEXT = "diff --git a/server.js b/server.js\n+autoescape = true\n"

GATE_A_CLEAN = (
    "Here is my review.\n"
    "```json\n"
    '{"verdict": "APPROVED", "findings": []}\n'
    "```\n"
)
GATE_A_HIGH = (
    "```json\n"
    '{"verdict": "CHANGES_REQUESTED", "findings": ['
    '{"severity": "high", "file": "server.js", "line": 1, '
    '"message": "SQL injection risk"}]}\n'
    "```\n"
)
GATE_A_LOW = (
    "```json\n"
    '{"verdict": "APPROVED", "findings": ['
    '{"severity": "low", "file": "server.js", "line": 2, '
    '"message": "Consider a clearer variable name."}]}\n'
    "```\n"
)


def _payload() -> dict:
    return {
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


@pytest.fixture
def fixed_workdir(monkeypatch):
    """Point tempfile.TemporaryDirectory at a tmp dir with a tests/ marker.

    Mirrors tests/test_agent.py: a ``tests/`` dir makes ``_detect_test_command``
    return 'pytest' deterministically. We avoid pytest's ``tmp_path`` because its
    path contains the substring 'pytest', which would collide with FakeRunner's
    substring match for the detected test command.
    """
    base = Path(tempfile.mkdtemp(prefix="kmogate-"))
    workdir = base / "work"
    workdir.mkdir()
    repo_dir = workdir / "your-repo"
    (repo_dir / "tests").mkdir(parents=True)

    @contextlib.contextmanager
    def _fake_tempdir(*args, **kwargs):
        yield str(workdir)

    monkeypatch.setattr(agent.tempfile, "TemporaryDirectory", _fake_tempdir)
    return workdir


@pytest.fixture(autouse=True)
def restore_runner():
    original = agent.RUNNER
    yield
    agent.RUNNER = original


@pytest.fixture
def gate_a_on(monkeypatch):
    """Turn the Gate A flag ON and Gate B OFF, then reload review_gates (agent
    does NOT own the flags, so it is NOT reloaded). Gates default ON now, so
    Gate B is explicitly disabled to isolate Gate A. Teardown restores the env
    and reloads review_gates back to its module-level default."""
    monkeypatch.setenv("SPEC_REVIEW_ENABLED", "1")
    monkeypatch.setenv("SPEC_VERIFY_SPEC_ENABLED", "0")
    importlib.reload(review_gates)
    try:
        assert review_gates.SPEC_REVIEW_ENABLED is True
        assert review_gates.SPEC_VERIFY_SPEC_ENABLED is False
        yield review_gates
    finally:
        monkeypatch.delenv("SPEC_REVIEW_ENABLED", raising=False)
        monkeypatch.delenv("SPEC_VERIFY_SPEC_ENABLED", raising=False)
        importlib.reload(review_gates)


def _success_results(review_verdicts: list[str],
                     pr_url: str = "https://github.com/o/r/pull/1",
                     *, diff: str = DIFF_TEXT) -> list[ScriptedResult]:
    """FakeRunner script for a one-shot fix that succeeds on the first attempt,
    then the post-fix gate phase.

    The single per-task CODING kiro pass and the Gate A review pass BOTH display
    'kiro-cli chat' identically, so they are scripted as ONE ordered FIFO matcher
    queue: the empty coding pass first, then the review verdict(s).
    """
    kiro_matchers = [ScriptedResult(stdout="", match="kiro-cli chat")]  # coding
    for verdict in review_verdicts:
        kiro_matchers.append(ScriptedResult(stdout=verdict, match="kiro-cli chat"))
    return [
        # Three scoped-staging status snapshots (post-clone, post-install clean,
        # post-fix reports the kiro-changed file).
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
        # Baseline + post-fix pytest both pass.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        # The cumulative diff for the gate phase (base..fix).
        ScriptedResult(stdout=diff, match="git diff master.."),
        # rc=1 => `git diff --cached --quiet` sees staged changes.
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
        # All kiro passes (coding + review) in call order.
        *kiro_matchers,
        ScriptedResult(stdout=pr_url, match="gh pr create"),
    ]


def _kiro_review_calls(runner: FakeRunner) -> list:
    """kiro-cli calls whose STDIN brief is a review brief (not a coding brief).
    Review briefs carry the Gate A schema language."""
    return [
        c for c in runner.calls
        if "kiro-cli chat" in c.display and c.input
        and "ADVERSARIAL code reviewer" in c.input
    ]


# ---------------------------------------------------------------------------
# (1) Gate A clean -> NON-draft PR with a ## Verification section.
# ---------------------------------------------------------------------------


def test_gate_a_clean_non_draft_with_verification_section(gate_a_on,
                                                          fixed_workdir):
    runner = FakeRunner(results=_success_results(
        [GATE_A_CLEAN], pr_url="https://github.com/o/r/pull/42"))
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["draft"] is False
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" in body
    assert "APPROVED" in body
    # Exactly ONE review pass (Gate A) on top of the single coding pass.
    assert len(_kiro_review_calls(runner)) == 1
    assert runner.count("kiro-cli chat") == 2
    # The cumulative base..fix diff was computed for the gate phase.
    assert runner.find("git diff master..") is not None


# ---------------------------------------------------------------------------
# (2) Gate A high finding -> one-shot PR DOWNGRADED to draft with the finding.
# ---------------------------------------------------------------------------


def test_gate_a_high_finding_downgrades_to_draft(gate_a_on, fixed_workdir):
    runner = FakeRunner(results=_success_results(
        [GATE_A_HIGH], pr_url="https://github.com/o/r/pull/9"))
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    # Gate-driven draft: RETRYABLE-ish tests_failed, commits kept/pushed.
    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert "Gate A" in result["reason"]
    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" in body
    assert "Gate A" in body
    assert "SQL injection risk" in body
    # Commits were still kept/pushed (ADR-008 Q4).
    assert runner.find("git commit") is not None
    assert runner.find("git push") is not None


# ---------------------------------------------------------------------------
# (3) Gate A low-only finding -> advisory, PR stays NON-draft (Q5).
# ---------------------------------------------------------------------------


def test_gate_a_low_finding_is_advisory_non_draft(gate_a_on, fixed_workdir):
    runner = FakeRunner(results=_success_results([GATE_A_LOW]))
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["draft"] is False
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" in body
    assert "clearer variable name" in body


# ---------------------------------------------------------------------------
# (4) Gate B is ALWAYS skip-with-notice on the one-shot path (no Kiro spec).
# ---------------------------------------------------------------------------


def test_gate_b_always_skipped_with_notice_on_one_shot(monkeypatch,
                                                       fixed_workdir):
    # Turn BOTH flags on; Gate B must STILL skip (acceptance_text is None on the
    # one-shot path) and never require a verdict.
    monkeypatch.setenv("SPEC_REVIEW_ENABLED", "1")
    monkeypatch.setenv("SPEC_VERIFY_SPEC_ENABLED", "1")
    importlib.reload(review_gates)
    try:
        runner = FakeRunner(results=_success_results([GATE_A_CLEAN]))
        agent.RUNNER = runner

        result = agent.run_pipeline(_payload())

        assert result["status"] == "success"
        assert result["draft"] is False
        gh = runner.find("gh pr create")
        body = gh.cmd[gh.cmd.index("--body") + 1]
        assert "## Verification" in body
        # Gate B skip-with-notice text is present; no Gate B verdict was needed.
        assert "Spec verification did not run" in body
        # Only ONE review pass (Gate A); Gate B never invoked kiro.
        assert len(_kiro_review_calls(runner)) == 1
        verify_calls = [
            c for c in runner.calls
            if c.input and "INDEPENDENT verifier" in c.input
        ]
        assert verify_calls == []
    finally:
        monkeypatch.delenv("SPEC_REVIEW_ENABLED", raising=False)
        monkeypatch.delenv("SPEC_VERIFY_SPEC_ENABLED", raising=False)
        importlib.reload(review_gates)


# ---------------------------------------------------------------------------
# (5) No secret sentinel leaks into any argv / STDIN / PR body.
# ---------------------------------------------------------------------------


def test_no_secret_leak_in_gate_phase(gate_a_on, fixed_workdir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    # Put the sentinel into the ticket description (which feeds the Gate A
    # intent) and into a gate finding message (which feeds the PR body).
    payload = _payload()
    payload["description"] = f"Fix leak with {TOKEN_SENTINEL} token"
    finding_verdict = (
        "```json\n"
        '{"verdict": "CHANGES_REQUESTED", "findings": ['
        f'{{"severity": "high", "message": "secret {KEY_SENTINEL} here"}}]}}\n'
        "```\n"
    )
    runner = FakeRunner(results=_success_results([finding_verdict]))
    agent.RUNNER = runner

    result = agent.run_pipeline(payload)

    assert result["draft"] is True
    # No sentinel in the result reason.
    assert TOKEN_SENTINEL not in result["reason"]
    assert KEY_SENTINEL not in result["reason"]
    # No sentinel anywhere in any recorded argv or STDIN brief.
    for rec in runner.calls:
        argv = rec.cmd if isinstance(rec.cmd, list) else [rec.cmd]
        joined = " ".join(str(a) for a in argv)
        assert TOKEN_SENTINEL not in joined
        assert KEY_SENTINEL not in joined
        if rec.input is not None:
            assert TOKEN_SENTINEL not in rec.input
            assert KEY_SENTINEL not in rec.input
    # The PR body is redacted by step_open_pr before gh sees it.
    gh = runner.find("gh pr create")
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert TOKEN_SENTINEL not in body
    assert KEY_SENTINEL not in body


# ---------------------------------------------------------------------------
# (6) FLAGS-OFF regression: one-shot PR path unchanged, NO extra review call.
# ---------------------------------------------------------------------------


def test_flags_off_one_shot_unchanged_no_review_call(fixed_workdir, monkeypatch):
    # Gates default ON now, so explicitly opt BOTH out to exercise the
    # flags-off regression path (one-shot PR path byte-for-byte unchanged).
    monkeypatch.setenv("SPEC_REVIEW_ENABLED", "0")
    monkeypatch.setenv("SPEC_VERIFY_SPEC_ENABLED", "0")
    importlib.reload(review_gates)
    assert review_gates.SPEC_REVIEW_ENABLED is False
    assert review_gates.SPEC_VERIFY_SPEC_ENABLED is False

    pr_url = "https://github.com/your-org/your-repo/pull/7"
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
        ScriptedResult(stdout=pr_url, match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["draft"] is False
    assert result["pr_url"] == pr_url
    # EXACTLY one kiro-cli pass (the coding pass); NO review pass happened.
    assert runner.count("kiro-cli chat") == 1
    assert _kiro_review_calls(runner) == []
    # NO cumulative base..fix diff was computed (gate phase skipped entirely).
    assert runner.find("git diff master..") is None
    # Non-draft PR, no Verification section in the body.
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" not in body
