"""Group E tests (#9 then #8): the real verifier.

Covers runtime/agent.py:

* ``_detect_install_command`` returns the right argv list per ecosystem and
  None when nothing matches.
* ``run_pipeline`` installs dependencies (argv list) and runs a BASELINE test
  pass BEFORE the first kiro-cli fix (ordering asserted via FakeRunner).
* Tests pass after fix  -> NON-draft ``gh pr create`` + status 'success'.
* Tests fail after all attempts -> ``gh pr create --draft`` + status
  'tests_failed'; the PR body / returned dict carry truncated failing output
  and the baseline result.
* No tests detected -> draft PR labelled for human review + tests_failed
  (NOT a silent success).
* The draft-PR body / argv never contain the GITHUB_TOKEN / KIRO_API_KEY
  sentinels.
* ``truncate_test_output`` keeps the first failing line within the budget.

Everything routes through FakeRunner / RecordingRunner so nothing real runs.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

import pytest

import agent  # conftest installs SDK stubs first
from fakes import FakeRunner, ScriptedResult


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"
KEY_SENTINEL = "kiro-key-SENTINEL-987654321"


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


@pytest.fixture(autouse=True)
def restore_runner():
    original = agent.RUNNER
    yield
    agent.RUNNER = original


def _make_workdir(monkeypatch, *, markers=("tests",)) -> Path:
    """Create a fake temp workdir whose repo_dir has the given marker files/dirs.

    ``markers`` entries ending in '/' (or named like dirs) are created as dirs;
    file markers (containing '.') are created as empty files. Returns the
    workdir; the pipeline computes repo_dir = workdir / 'your-repo'.
    """
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
# _detect_install_command
# ---------------------------------------------------------------------------


def test_detect_install_npm_ci_with_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    assert agent._detect_install_command(tmp_path) == ["npm", "ci"]


def test_detect_install_npm_ci_with_shrinkwrap(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "npm-shrinkwrap.yaml").write_text("")
    assert agent._detect_install_command(tmp_path) == ["npm", "ci"]


def test_detect_install_npm_install_without_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert agent._detect_install_command(tmp_path) == ["npm", "install"]


def test_detect_install_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    assert agent._detect_install_command(tmp_path) == [
        "pip", "install", "-r", "requirements.txt",
    ]


def test_detect_install_requirements_glob(tmp_path):
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    assert agent._detect_install_command(tmp_path) == [
        "pip", "install", "-r", "requirements-dev.txt",
    ]


def test_detect_install_pyproject_test_extra(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\ntest = [\"pytest\"]\n"
    )
    assert agent._detect_install_command(tmp_path) == [
        "pip", "install", "-e", ".[test]",
    ]


def test_detect_install_setup_py_test_extra(tmp_path):
    (tmp_path / "setup.py").write_text(
        "setup(extras_require={'test': ['pytest']})\n"
    )
    assert agent._detect_install_command(tmp_path) == [
        "pip", "install", "-e", ".[test]",
    ]


def test_detect_install_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert agent._detect_install_command(tmp_path) == [
        "mvn", "-q", "dependency:resolve",
    ]


def test_detect_install_none(tmp_path):
    assert agent._detect_install_command(tmp_path) is None


# ---------------------------------------------------------------------------
# truncate_test_output
# ---------------------------------------------------------------------------


def test_truncate_keeps_short_output_unchanged():
    out = "short output"
    assert agent.truncate_test_output(out) == out


def test_truncate_budget_and_first_failure_preserved():
    # First failure early, then a very long tail that would be dropped.
    first = "tests/test_x.py::test_a FAILED"
    body = first + "\n" + ("x" * 5000) + "\nZZZEND"
    out = agent.truncate_test_output(body, budget=500)
    assert len(out) <= 500 + len("\nFIRST FAILURE: ") + len(first) + 50
    assert "FAILED" in out  # the failing signal survives
    assert "ZZZEND" in out  # the tail is retained


def test_truncate_surfaces_first_failure_when_buried():
    head = "y" * 4000
    first = "AssertionError: boom here"
    body = head + "\n" + first + "\n" + ("z" * 4000)
    out = agent.truncate_test_output(body, budget=200)
    assert "FIRST FAILURE:" in out
    assert "AssertionError: boom here" in out


# ---------------------------------------------------------------------------
# run_pipeline: install + baseline ordering (#9)
# ---------------------------------------------------------------------------


def test_install_and_baseline_run_before_first_fix(monkeypatch):
    # Repo has package.json + lockfile (npm ci) and a tests/ dir (pytest).
    _make_workdir(monkeypatch, markers=("tests", "package.json", "package-lock.json"))

    pr_url = "https://github.com/your-org/your-repo/pull/9"
    runner = FakeRunner(
        results=[
            # Scoped staging (#13): three porcelain snapshots (post-clone,
            # post-install, post-fix); only the last reports a kiro-changed file.
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout=pr_url, match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())
    assert result["status"] == "success"

    cmds = runner.commands
    install_idx = next(i for i, c in enumerate(cmds) if c == "npm ci")
    # npm test is the detected test command for a package.json repo.
    first_test_idx = next(i for i, c in enumerate(cmds) if c == "npm test")
    kiro_idx = next(i for i, c in enumerate(cmds) if "kiro-cli chat" in c)

    # Install happens before the baseline test run, which happens before the
    # first kiro-cli fix.
    assert install_idx < first_test_idx < kiro_idx
    # Baseline recorded.
    assert result["baseline"]["passed"] is True


def test_install_command_is_argv_list(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests", "requirements.txt"))
    runner = FakeRunner(
        results=[
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/1", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner
    agent.run_pipeline(_payload())

    install = runner.find("pip install -r requirements.txt")
    assert install is not None
    assert isinstance(install.cmd, list)
    assert install.check is False
    assert install.timeout == agent.INSTALL_TIMEOUT_S


# ---------------------------------------------------------------------------
# PR branching (#8)
# ---------------------------------------------------------------------------


def test_tests_pass_opens_non_draft_pr(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    pr_url = "https://github.com/your-org/your-repo/pull/10"
    runner = FakeRunner(
        results=[
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout=pr_url, match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["draft"] is False
    gh = runner.find("gh pr create")
    assert gh is not None
    assert "--draft" not in gh.cmd


def test_tests_fail_opens_draft_pr_with_output_and_baseline(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    # Group F (#12) stops early when two consecutive attempts share a failure
    # fingerprint, so to exhaust ALL attempts (the case this test characterizes)
    # each attempt must fail on a DIFFERENT test id (distinct fingerprints).
    runner = FakeRunner(
        results=[
            # baseline pytest -> green; every subsequent pytest -> fail.
            ScriptedResult(returncode=0, stdout="baseline ok", match="pytest"),
            ScriptedResult(returncode=1,
                           stdout="FAILED tests/test_x.py::test_a\nAssertionError: a",
                           match="pytest"),
            ScriptedResult(returncode=1,
                           stdout="FAILED tests/test_x.py::test_b\nAssertionError: b",
                           match="pytest"),
            ScriptedResult(returncode=1,
                           stdout="FAILED tests/test_x.py::test_c\nAssertionError: c",
                           match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/11", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert result["tests_passed"] is False
    assert result["baseline"]["passed"] is True
    assert "FAILED" in result["test_output"]
    assert result["attempts"] == agent.MAX_FIX_ATTEMPTS

    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    # The failing output is embedded in the PR body argv.
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "FAILED" in body
    assert "Baseline" in body


def test_no_tests_detected_opens_draft_for_human_review(monkeypatch):
    # No tests/ marker and no ecosystem files => no test runner detected.
    _make_workdir(monkeypatch, markers=())
    runner = FakeRunner(
        results=[
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/12", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    # NOT a silent success.
    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert result["no_tests"] is True
    assert result["tests_passed"] is False

    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    assert "--label" in gh.cmd
    assert gh.cmd[gh.cmd.index("--label") + 1] == "needs-human-review"
    # kiro-cli still ran exactly once (the fix is applied but unverified).
    assert runner.count("kiro-cli chat") == 1
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "NO TESTS DETECTED" in body


# ---------------------------------------------------------------------------
# Secret hygiene in the draft PR body
# ---------------------------------------------------------------------------


def test_draft_pr_body_and_argv_redact_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    _make_workdir(monkeypatch, markers=("tests",))

    # Failing output that leaks both sentinels; the body must redact them.
    leaky = f"FAILED with token {TOKEN_SENTINEL} and key {KEY_SENTINEL}"
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=0, stdout="baseline ok", match="pytest"),
            ScriptedResult(returncode=1, stdout=leaky, match="pytest"),
            ScriptedResult(returncode=1, stdout=leaky, match="pytest"),
            ScriptedResult(returncode=1, stdout=leaky, match="pytest"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout="", match="git status --porcelain"),
            ScriptedResult(stdout=" M server.js\n", match="git status --porcelain"),
            ScriptedResult(returncode=1, match="git diff --cached --quiet"),
            ScriptedResult(stdout="https://github.com/o/r/pull/13", match="gh pr create"),
        ]
    )
    agent.RUNNER = runner

    agent.run_pipeline(_payload())

    gh = runner.find("gh pr create")
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert TOKEN_SENTINEL not in body
    assert KEY_SENTINEL not in body
    assert agent.REDACTION_MASK in body
    # No sentinel anywhere in the whole gh argv.
    assert all(TOKEN_SENTINEL not in str(a) for a in gh.cmd)
    assert all(KEY_SENTINEL not in str(a) for a in gh.cmd)
