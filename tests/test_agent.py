"""Characterization tests for the runtime deterministic pipeline (agent.py).

The pipeline's subprocess calls are routed through ``agent.RUNNER``; we replace
it with a ``FakeRunner`` so nothing real is spawned. We characterize the happy
path, the retry-then-pass path, and the no-diff failure path exactly as the
current code behaves. No bugs are fixed here.

Never assert on / log GITHUB_TOKEN or KIRO_API_KEY.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest

import agent  # works via the bedrock_agentcore shim in conftest.py
from fakes import FakeRunner, ScriptedResult


# PROJ-15/SW15 shaped payload (no secrets).
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
    """Point tempfile.TemporaryDirectory at a real tmp dir with repo markers.

    The pipeline computes ``repo_dir = workdir / repo_name`` and
    ``_detect_test_command`` inspects that directory, so we pre-create the repo
    dir with a ``tests/`` marker to make detection deterministic (-> 'pytest').

    We deliberately avoid pytest's ``tmp_path`` here: its path contains the
    substring "pytest", which would collide with FakeRunner's substring match
    for the detected ``pytest`` test command.
    """
    base = Path(tempfile.mkdtemp(prefix="kmtest-"))
    workdir = base / "work"
    workdir.mkdir()
    repo_dir = workdir / "your-repo"
    (repo_dir / "tests").mkdir(parents=True)

    @contextlib.contextmanager
    def _fake_tempdir(*args, **kwargs):
        yield str(workdir)

    monkeypatch.setattr(agent.tempfile, "TemporaryDirectory", _fake_tempdir)
    try:
        yield workdir
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def restore_runner():
    """Ensure agent.RUNNER is restored after each test."""
    original = agent.RUNNER
    yield
    agent.RUNNER = original


def test_happy_path(fixed_workdir):
    pr_url = "https://github.com/your-org/your-repo/pull/42"
    runner = FakeRunner(
        results=[
            # Scoped staging (#13) takes three `git status --porcelain` snapshots
            # (post-clone, post-install, post-fix), consumed in order. The first
            # two are clean; the post-fix one reports one kiro-changed file plus
            # a cache artifact that must be excluded.
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(
                stdout=" M server.js\n?? __pycache__/x.pyc\n",
                match="git status --porcelain",
            ),
            # rc=1 means `git diff --cached --quiet` sees staged changes.
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout=pr_url, match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["pr_url"] == pr_url
    assert result["attempts"] == 1
    assert result["tests_passed"] is True
    # Only the kiro-changed source file is staged; the cache artifact is excluded.
    assert result["staged_files"] == ["server.js"]

    cmds = runner.commands
    # Expected ordered sequence of key commands.
    assert any(c.startswith("git clone --branch master --single-branch") for c in cmds)
    assert "git checkout -b sonarqube-fix/xss-swig-autoescape" in cmds
    # Scoped staging replaced the old blanket `git add -A` (#13).
    assert "git add -A" not in cmds
    assert "git status --porcelain" in cmds
    # The surviving path is staged via `git add -- <path>` (not the excluded one).
    add = runner.find("git add --")
    assert add is not None
    assert add.cmd == ["git", "add", "--", "server.js"]
    assert "git diff --cached --quiet" in cmds
    assert "git push origin sonarqube-fix/xss-swig-autoescape" in cmds

    # kiro-cli is invoked as a LIST argv (shell=False path).
    kiro = runner.find("kiro-cli chat")
    assert isinstance(kiro.cmd, list)
    assert kiro.cmd[:4] == ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"]

    # A test command was detected and run (pytest, from the tests/ marker).
    assert runner.find("pytest") is not None

    # git commit runs as a list argv; gh pr create as a list argv.
    commit = runner.find("git commit")
    assert isinstance(commit.cmd, list)
    gh = runner.find("gh pr create")
    assert isinstance(gh.cmd, list)


def test_retry_then_pass(fixed_workdir):
    # Ordered pytest matchers are consumed in sequence (#9 adds a baseline run):
    #   1) baseline run  -> rc=1 (red before the fix)
    #   2) attempt 1 run  -> rc=1 (still failing, triggers a retry)
    #   3) attempt 2 run  -> rc=0 (green, break)
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=1, stdout="baseline failed", match="pytest"),
            ScriptedResult(returncode=1, stdout="1 failed", match="pytest"),
            ScriptedResult(returncode=0, stdout="all passed", match="pytest"),
            # Scoped staging (#13) takes three `git status --porcelain` snapshots
            # (post-clone, post-install, post-fix), consumed in order: the first
            # two are clean, the post-fix one reports one kiro-changed file.
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            # rc=1 means `git diff --cached --quiet` sees staged changes.
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/7", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["attempts"] == 2
    # Baseline was red (failing before the fix).
    assert result["baseline"]["passed"] is False

    # Reset commands issued between the failed attempt and the retry.
    assert "git checkout -- ." in runner.commands
    assert "git clean -fd" in runner.commands
    # kiro-cli ran twice (once per attempt).
    assert runner.count("kiro-cli chat") == 2


def test_no_diff_path(fixed_workdir):
    # git diff --cached --quiet returns rc==0 -> no changes to commit.
    runner = FakeRunner(
        results=[ScriptedResult(returncode=0, match="git diff --cached --quiet")]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "failed"
    assert result["reason"] == "kiro-cli produced no changes"
    # No push or PR happened.
    assert runner.find("git push") is None
    assert runner.find("gh pr create") is None
