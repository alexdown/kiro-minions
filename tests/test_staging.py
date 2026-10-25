"""Group G tests (#13): scoped staging in runtime/agent.py.

Covers:

* ``_git_status_porcelain`` parses ``git status --porcelain`` into a path set
  (handling status codes, untracked ``??`` entries, renames, and quoted paths),
  and returns an empty set on a non-zero git exit.
* ``_is_excluded_path`` / ``_paths_to_stage`` exclude common build / coverage /
  cache directories and files and only surface paths kiro changed SINCE the
  post-install snapshot.
* ``step_commit`` stages ONLY the kiro-changed paths via ``git add -- <paths>``
  (never ``git add -A``), skips install-churned and excluded artifacts, and
  returns ``(False, [])`` when nothing survives the exclusion filter.
* ``run_pipeline`` snapshots porcelain after clone AND after install, populates
  the ``staged_files`` result field, and ``step_open_pr`` lists them in the PR
  body for BOTH normal and draft PRs.
* No GITHUB_TOKEN / KIRO_API_KEY sentinel leaks into any argv or the PR body.

Everything routes through FakeRunner so nothing real runs.
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


def _status_snapshots(*outputs: str) -> list[ScriptedResult]:
    """Ordered `git status --porcelain` matchers returning each output in turn."""
    return [ScriptedResult(stdout=out, match="git status --porcelain")
            for out in outputs]


# ---------------------------------------------------------------------------
# _git_status_porcelain parsing
# ---------------------------------------------------------------------------


def test_porcelain_parses_paths_and_untracked():
    runner = FakeRunner(results=[ScriptedResult(
        stdout=" M src/app.py\n?? newfile.txt\nA  added.py\n",
        match="git status --porcelain",
    )])
    agent.RUNNER = runner
    paths = agent._git_status_porcelain(Path("/tmp/repo"))
    assert paths == {"src/app.py", "newfile.txt", "added.py"}


def test_porcelain_parses_rename_keeps_destination():
    runner = FakeRunner(results=[ScriptedResult(
        stdout="R  old/name.py -> new/name.py\n",
        match="git status --porcelain",
    )])
    agent.RUNNER = runner
    paths = agent._git_status_porcelain(Path("/tmp/repo"))
    assert paths == {"new/name.py"}


def test_porcelain_strips_quoted_paths():
    runner = FakeRunner(results=[ScriptedResult(
        stdout=' M "path with spaces.py"\n',
        match="git status --porcelain",
    )])
    agent.RUNNER = runner
    paths = agent._git_status_porcelain(Path("/tmp/repo"))
    assert paths == {"path with spaces.py"}


def test_porcelain_empty_on_nonzero_exit():
    runner = FakeRunner(results=[ScriptedResult(
        returncode=128, stdout="", stderr="not a git repo",
        match="git status --porcelain",
    )])
    agent.RUNNER = runner
    # check=False inside the helper; a non-zero rc yields an EMPTY set so a
    # transient failure never mis-attributes artifacts to kiro.
    assert agent._git_status_porcelain(Path("/tmp/repo")) == set()


# ---------------------------------------------------------------------------
# exclusion + diff logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "node_modules/left-pad/index.js",
    ".pytest_cache/v/cache/lastfailed",
    "src/__pycache__/app.cpython-311.pyc",
    "app.pyc",
    "coverage/lcov.info",
    ".coverage",
    "htmlcov/index.html",
    "dist/pkg-1.0.tar.gz",
    "build/lib/mod.py",
    "target/classes/App.class",
    "mypkg.egg-info/PKG-INFO",
    ".mypy_cache/x",
    ".ruff_cache/y",
    ".tox/py311/log",
    ".venv/bin/python",
    "venv/lib/site-packages/z.py",
])
def test_excluded_paths(path):
    assert agent._is_excluded_path(path) is True


@pytest.mark.parametrize("path", [
    "src/app.py",
    "server.js",
    "README.md",
    "tests/test_app.py",
    "pkg/module.py",
])
def test_non_excluded_paths(path):
    assert agent._is_excluded_path(path) is False


def test_paths_to_stage_only_new_kiro_changes():
    # requirements.txt was already churned by install (post_install); server.js
    # and src/app.py are new kiro changes; node_modules/.pytest_cache excluded.
    post_install = {"requirements.txt", "node_modules/a.js"}
    post_fix = {
        "requirements.txt",            # already dirty pre-fix -> NOT staged
        "node_modules/a.js",           # excluded + pre-existing
        "server.js",                   # new kiro change -> staged
        "src/app.py",                  # new kiro change -> staged
        ".pytest_cache/lastfailed",    # excluded artifact -> NOT staged
        "__pycache__/x.pyc",           # excluded artifact -> NOT staged
    }
    assert agent._paths_to_stage(post_install, post_fix) == ["server.js", "src/app.py"]


# ---------------------------------------------------------------------------
# step_commit: scoped staging
# ---------------------------------------------------------------------------


def test_step_commit_stages_only_kiro_paths(monkeypatch):
    runner = FakeRunner(results=[
        # post-fix status: one real change + an excluded cache artifact.
        ScriptedResult(stdout=" M server.js\n?? __pycache__/x.pyc\n",
                       match="git status --porcelain"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
    ])
    agent.RUNNER = runner

    had_changes, staged = agent.step_commit(
        Path("/tmp/repo"), "XSS fix", "PROJ-15", "hash", post_install=set(),
    )

    assert had_changes is True
    assert staged == ["server.js"]

    add = runner.find("git add --")
    assert add is not None
    # Only the kiro file is staged, after the `--` separator; cache is excluded.
    assert add.cmd == ["git", "add", "--", "server.js"]
    # The old blanket `git add -A` is gone.
    assert runner.find("git add -A") is None


def test_step_commit_excludes_install_churn(monkeypatch):
    # The lockfile was dirtied by `npm install` (post_install); only app.js is
    # kiro's change and should be staged.
    runner = FakeRunner(results=[
        ScriptedResult(stdout=" M package-lock.json\n M app.js\n",
                       match="git status --porcelain"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
    ])
    agent.RUNNER = runner

    had_changes, staged = agent.step_commit(
        Path("/tmp/repo"), "fix", "PROJ-1", "h",
        post_install={"package-lock.json"},
    )

    assert had_changes is True
    assert staged == ["app.js"]
    add = runner.find("git add --")
    assert add.cmd == ["git", "add", "--", "app.js"]


def test_step_commit_no_paths_after_exclusion_returns_false(monkeypatch):
    # Everything dirty is either install churn or an excluded artifact: nothing
    # to stage -> (False, []) and NO `git add` / `git commit` happen.
    runner = FakeRunner(results=[
        ScriptedResult(stdout=" M node_modules/a.js\n?? .pytest_cache/x\n",
                       match="git status --porcelain"),
    ])
    agent.RUNNER = runner

    had_changes, staged = agent.step_commit(
        Path("/tmp/repo"), "fix", "PROJ-1", "h", post_install=set(),
    )

    assert had_changes is False
    assert staged == []
    assert runner.find("git add") is None
    assert runner.find("git commit") is None


# ---------------------------------------------------------------------------
# run_pipeline: staged_files surfaced + in PR body (normal + draft)
# ---------------------------------------------------------------------------


def test_pipeline_populates_staged_files_and_normal_pr_body(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    runner = FakeRunner(results=[
        # post-clone (clean), post-install (clean), post-fix (one file + cache).
        *_status_snapshots("", "", " M server.js\n?? __pycache__/x.pyc\n"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
        ScriptedResult(stdout="https://github.com/o/r/pull/1", match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "success"
    assert result["staged_files"] == ["server.js"]

    # `git add -A` is gone; scoped `git add --` is used instead.
    assert "git add -A" not in runner.commands
    add = runner.find("git add --")
    assert add.cmd == ["git", "add", "--", "server.js"]

    # The staged file appears in the (normal, non-draft) PR body.
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "Staged files" in body
    assert "`server.js`" in body
    # The excluded cache artifact never reaches the body.
    assert "__pycache__" not in body


def test_pipeline_lists_staged_files_in_draft_pr_body(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    # Tests fail after all attempts -> draft PR. Distinct fingerprints per
    # attempt so the loop exhausts MAX_FIX_ATTEMPTS instead of stopping early.
    runner = FakeRunner(results=[
        ScriptedResult(returncode=0, stdout="baseline ok", match="pytest"),
        ScriptedResult(returncode=1, stdout="FAILED tests/t.py::test_a", match="pytest"),
        ScriptedResult(returncode=1, stdout="FAILED tests/t.py::test_b", match="pytest"),
        ScriptedResult(returncode=1, stdout="FAILED tests/t.py::test_c", match="pytest"),
        *_status_snapshots("", "", " M server.js\n"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
        ScriptedResult(stdout="https://github.com/o/r/pull/2", match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert result["staged_files"] == ["server.js"]

    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "Staged files" in body
    assert "`server.js`" in body


def test_pipeline_no_add_dash_A_anywhere(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    runner = FakeRunner(results=[
        *_status_snapshots("", "", " M server.js\n"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
        ScriptedResult(stdout="https://github.com/o/r/pull/3", match="gh pr create"),
    ])
    agent.RUNNER = runner

    agent.run_pipeline(_payload())

    # Across the whole pipeline, no recorded command is the blanket `git add -A`.
    assert all("git add -A" not in c for c in runner.commands)


def test_pipeline_excluded_only_changes_yield_no_diff(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    # All post-fix changes are build/cache artifacts -> nothing to stage ->
    # the pipeline reports the no-change failed path (no push / no PR).
    runner = FakeRunner(results=[
        *_status_snapshots("", "",
                           " M node_modules/a.js\n?? .pytest_cache/x\n"),
    ])
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())

    assert result["status"] == "failed"
    assert result["reason"] == "kiro-cli produced no changes"
    assert result["staged_files"] == []
    assert runner.find("git push") is None
    assert runner.find("gh pr create") is None


# ---------------------------------------------------------------------------
# secret hygiene
# ---------------------------------------------------------------------------


def test_no_secret_in_staging_argv_or_pr_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    _make_workdir(monkeypatch, markers=("tests",))
    runner = FakeRunner(results=[
        *_status_snapshots("", "", " M server.js\n"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
        ScriptedResult(stdout="https://github.com/o/r/pull/4", match="gh pr create"),
    ])
    agent.RUNNER = runner

    agent.run_pipeline(_payload())

    for call in runner.calls:
        parts = call.cmd if isinstance(call.cmd, list) else [call.cmd]
        blob = "\n".join(str(p) for p in parts)
        assert TOKEN_SENTINEL not in blob
        assert KEY_SENTINEL not in blob
