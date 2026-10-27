"""FEAT-002 tests: the spec-executor worker (runtime/spec_agent.py).

The spec-executor runs a NEW deterministic OUTER loop that reads a WHOLE
tasks.md and iterates task-by-task with a FRESH per-task kiro-cli pass, one
commit per completed task on a single fix branch, one PR per spec, and
stop-at-first-blocker. It REUSES runtime/agent.py's building blocks and goes
through agent.RUNNER so nothing real runs.

Coverage:
  * happy path: a 3-task tasks.md all pass -> non-draft PR, 3 task commits (each
    naming its task), status success;
  * stop-at-first-blocker: task 2 of 3 keeps failing -> loop stops, task 3 is
    NEVER sent to kiro, ONE draft PR naming the blocked task + listing task 1;
  * already-[x] tasks are skipped;
  * budget: SPEC_MAX_TASKS=1 stops after one task with a draft PR + budget reason;
  * secret hygiene: no sentinel in any argv / stdin / PR body;
  * tasks.md read from a repo-relative path works; a traversal path is rejected;
  * entrypoint routing: task_type='spec' drives run_spec_pipeline and writes the
    terminal DynamoDB status; task_type absent/one_shot keeps run_pipeline.

Follows the FakeRunner/ScriptedResult match-substring contract and the fake
temp-workdir pattern from tests/test_retry_loop.py / tests/test_staging.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import tempfile
from pathlib import Path

import pytest

import agent  # conftest installs SDK stubs first
import spec_agent
from fakes import FakeDynamoResource, FakeRunner, ScriptedResult


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"
KEY_SENTINEL = "kiro-key-SENTINEL-987654321"

THREE_TASKS = """# Implementation Plan

- [ ] 1. First task
  - do the first thing
  - _Requirements: 1.1_
- [ ] 2. Second task
  - do the second thing
- [ ] 3. Third task
  - do the third thing
"""


def _payload(**overrides) -> dict:
    p = {
        "repo_clone_url": "https://github.com/your-org/your-repo.git",
        "base_branch": "master",
        "fix_branch": "spec/my-feature",
        "summary": "My feature",
        "sonar_hash": "ide-6f50f92a65f2",
        "task_type": "spec",
        "tasks_md": THREE_TASKS,
    }
    p.update(overrides)
    return p


@pytest.fixture(autouse=True)
def restore_runner():
    original = agent.RUNNER
    yield
    agent.RUNNER = original


def _make_workdir(monkeypatch, *, markers=("tests",),
                  tasks_md_file: tuple[str, str] | None = None) -> Path:
    """Create a fake temp workdir whose repo_dir has the given marker dirs/files.

    ``tasks_md_file`` optionally writes a (relative_path, content) file into the
    repo so a tasks_md_path read can be exercised against a real on-disk file.
    """
    base = Path(tempfile.mkdtemp(prefix="kmspec-"))
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
    if tasks_md_file is not None:
        rel, content = tasks_md_file
        dest = repo_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    @contextlib.contextmanager
    def _fake_tempdir(*args, **kwargs):
        yield str(workdir)

    monkeypatch.setattr(agent.tempfile, "TemporaryDirectory", _fake_tempdir)
    monkeypatch.setattr(spec_agent.tempfile, "TemporaryDirectory", _fake_tempdir)
    return workdir


def _commit_cycle(status_after: str) -> list[ScriptedResult]:
    """One task's commit cycle: post-fix porcelain snapshot + diff guard.

    ``step_commit`` snapshots `git status --porcelain` (post-fix) and then runs
    `git diff --cached --quiet` (rc=1 => something staged) before `git commit`.
    """
    return [
        ScriptedResult(stdout=status_after, match="git status --porcelain"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
    ]


# ---------------------------------------------------------------------------
# (1) HAPPY PATH
# ---------------------------------------------------------------------------


def test_happy_path_three_tasks_non_draft_pr(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    # Baseline snapshot (post-install) is clean. Each task: kiro + pytest pass,
    # then its commit cycle, then the advance-baseline snapshot.
    runner = FakeRunner(results=[
        # Post-install staging baseline.
        ScriptedResult(stdout="", match="git status --porcelain"),
        # Task 1.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        # Task 2.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n M b.py\n"),
        ScriptedResult(stdout=" M a.py\n M b.py\n", match="git status --porcelain"),
        # Task 3.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n M b.py\n M c.py\n"),
        ScriptedResult(stdout=" M a.py\n M b.py\n M c.py\n",
                       match="git status --porcelain"),
        # Push + PR.
        ScriptedResult(stdout="https://github.com/o/r/pull/42", match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = spec_agent.run_spec_pipeline(_payload())

    assert result["status"] == "success"
    assert result["draft"] is False
    assert result["pr_url"] == "https://github.com/o/r/pull/42"
    assert result["tasks"]["completed"] == ["1", "2", "3"]
    assert result["tasks"]["blocked"] is None

    # kiro ran once per task (3); commit ran once per completed task (3).
    assert runner.count("kiro-cli chat") == 3
    assert runner.count("git commit") == 3

    # Each commit message names its task.
    commits = [c for c in runner.calls if "git commit" in c.display]
    assert len(commits) == 3
    joined = "\n".join(c.display for c in commits)
    assert "1 First task" in joined
    assert "2 Second task" in joined
    assert "3 Third task" in joined

    # Non-draft PR.
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "First task" in body and "Third task" in body


def test_happy_path_each_kiro_brief_is_fresh_per_task(monkeypatch):
    """The per-task brief carries ONLY that task + spec context, via STDIN."""
    _make_workdir(monkeypatch, markers=("tests",))
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n M b.py\n"),
        ScriptedResult(stdout=" M a.py\n M b.py\n", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n M b.py\n M c.py\n"),
        ScriptedResult(stdout=" M a.py\n M b.py\n M c.py\n",
                       match="git status --porcelain"),
        ScriptedResult(stdout="https://github.com/o/r/pull/1", match="gh pr create"),
    ])
    agent.RUNNER = runner

    spec_agent.run_spec_pipeline(_payload())

    kiro_calls = [c for c in runner.calls if "kiro-cli chat" in c.display]
    assert len(kiro_calls) == 3
    # The brief is on STDIN, never argv.
    for c in kiro_calls:
        assert c.cmd == ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"]
        assert c.input is not None
    # Task 2's brief names the second task but NOT the first (fresh context).
    second = kiro_calls[1].input
    assert "Second task" in second
    assert "First task" not in second


# ---------------------------------------------------------------------------
# (2) STOP-AT-FIRST-BLOCKER
# ---------------------------------------------------------------------------


def test_stop_at_first_blocker_opens_draft_pr(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    # Task 1 passes + commits. Task 2 fails every attempt (same fingerprint so
    # the per-task loop stops at 2 attempts). Task 3 is NEVER attempted.
    fail = "FAILED tests/test_x.py::test_a\nAssertionError: broken"
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        # Task 1 passes.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        # Task 2: every pytest fails with the same output -> blocker.
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        # Push + draft PR.
        ScriptedResult(stdout="https://github.com/o/r/pull/9", match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = spec_agent.run_spec_pipeline(_payload())

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert result["tasks"]["completed"] == ["1"]
    assert result["tasks"]["blocked"]["id"] == "2"

    # kiro ran for task 1 and task 2 but NOT task 3.
    assert runner.count("kiro-cli chat") >= 2
    # Only one commit (task 1).
    assert runner.count("git commit") == 1

    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    # Body lists completed task 1 and names the blocked task 2.
    assert "First task" in body
    assert "Second task" in body
    assert "Blocked task" in body
    # Task 3 never reached the body as completed.
    assert "Third task" not in body


def test_blocker_on_first_task_with_no_prior_work_opens_no_pr(monkeypatch):
    """A blocker with zero completed tasks returns failed WITHOUT an empty PR."""
    _make_workdir(monkeypatch, markers=("tests",))
    fail = "FAILED tests/test_x.py::test_a\nAssertionError: broken"
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
    ])
    agent.RUNNER = runner

    result = spec_agent.run_spec_pipeline(_payload())

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert result["pr_url"] == ""
    assert result["tasks"]["completed"] == []
    assert runner.find("git push") is None
    assert runner.find("gh pr create") is None


# ---------------------------------------------------------------------------
# (3) ALREADY-DONE tasks are skipped
# ---------------------------------------------------------------------------


def test_already_done_tasks_are_skipped(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    tasks = (
        "- [x] 1. Done already\n"
        "- [ ] 2. Needs work\n"
    )
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        # Only task 2 should reach kiro + pytest + commit.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M b.py\n"),
        ScriptedResult(stdout=" M b.py\n", match="git status --porcelain"),
        ScriptedResult(stdout="https://github.com/o/r/pull/3", match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = spec_agent.run_spec_pipeline(_payload(tasks_md=tasks))

    assert result["status"] == "success"
    # kiro ran exactly once (only the not-done task).
    assert runner.count("kiro-cli chat") == 1
    assert result["tasks"]["completed"] == ["2"]
    # The done task's title never went to kiro.
    kiro = runner.find("kiro-cli chat")
    assert "Done already" not in (kiro.input or "")
    assert "Needs work" in (kiro.input or "")


# ---------------------------------------------------------------------------
# (4) BUDGET: SPEC_MAX_TASKS stops the loop early
# ---------------------------------------------------------------------------


def test_spec_max_tasks_budget_stops_after_one_task(monkeypatch):
    monkeypatch.setenv("SPEC_MAX_TASKS", "1")
    reloaded = importlib.reload(spec_agent)
    try:
        assert reloaded.SPEC_MAX_TASKS == 1
        _make_workdir(monkeypatch, markers=("tests",))
        monkeypatch.setattr(reloaded.tempfile, "TemporaryDirectory",
                            agent.tempfile.TemporaryDirectory)
        runner = FakeRunner(results=[
            ScriptedResult(stdout="", match="git status --porcelain"),
            # Task 1 completes.
            ScriptedResult(returncode=0, stdout="ok", match="pytest"),
            *_commit_cycle(" M a.py\n"),
            ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
            # Budget hit before task 2 -> draft PR.
            ScriptedResult(stdout="https://github.com/o/r/pull/5", match="gh pr create"),
        ])
        agent.RUNNER = runner

        result = reloaded.run_spec_pipeline(_payload())

        assert result["status"] == agent.STATUS_TESTS_FAILED
        assert result["draft"] is True
        assert result["tasks"]["completed"] == ["1"]
        assert result["tasks"]["blocked"]["id"] == "2"
        assert "SPEC_MAX_TASKS" in result["tasks"]["blocked"]["reason"]
        # Only one task ran through kiro.
        assert runner.count("kiro-cli chat") == 1
    finally:
        monkeypatch.delenv("SPEC_MAX_TASKS", raising=False)
        importlib.reload(spec_agent)


# ---------------------------------------------------------------------------
# (5) SECRET HYGIENE
# ---------------------------------------------------------------------------


def test_no_secret_in_argv_stdin_or_pr_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    _make_workdir(monkeypatch, markers=("tests",))
    tasks = (
        f"- [ ] 1. Use {TOKEN_SENTINEL} somewhere\n"
        "- [ ] 2. Also handle keys\n"
    )
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n M b.py\n"),
        ScriptedResult(stdout=" M a.py\n M b.py\n", match="git status --porcelain"),
        ScriptedResult(stdout="https://github.com/o/r/pull/8", match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = spec_agent.run_spec_pipeline(_payload(
        summary=f"Feature using {KEY_SENTINEL}",
        tasks_md=tasks,
    ))
    assert result["status"] == "success"

    for call in runner.calls:
        parts = call.cmd if isinstance(call.cmd, list) else [call.cmd]
        blob = "\n".join(str(p) for p in parts)
        if call.input:
            blob += "\n" + call.input
        assert TOKEN_SENTINEL not in blob
        assert KEY_SENTINEL not in blob


# ---------------------------------------------------------------------------
# (6) tasks.md read from a repo-relative path + traversal rejection
# ---------------------------------------------------------------------------


def test_tasks_md_read_from_repo_relative_path(monkeypatch):
    _make_workdir(
        monkeypatch, markers=("tests",),
        tasks_md_file=(".kiro/specs/feature/tasks.md", "- [ ] 1. Only task\n"),
    )
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        ScriptedResult(stdout="https://github.com/o/r/pull/6", match="gh pr create"),
    ])
    agent.RUNNER = runner

    payload = _payload()
    del payload["tasks_md"]
    payload["tasks_md_path"] = ".kiro/specs/feature/tasks.md"

    result = spec_agent.run_spec_pipeline(payload)
    assert result["status"] == "success"
    assert result["tasks"]["completed"] == ["1"]


@pytest.mark.parametrize("bad_path", [
    "../../etc/passwd",
    "/etc/passwd",
    "a/../../b",
    "-rf",
])
def test_traversal_tasks_md_path_rejected(bad_path):
    with pytest.raises(ValueError):
        spec_agent._validate_tasks_md_path(bad_path)


def test_missing_tasks_source_raises(monkeypatch):
    _make_workdir(monkeypatch, markers=("tests",))
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
    ])
    agent.RUNNER = runner
    payload = _payload()
    del payload["tasks_md"]
    with pytest.raises(ValueError):
        spec_agent.run_spec_pipeline(payload)


# ---------------------------------------------------------------------------
# Entrypoint routing
# ---------------------------------------------------------------------------


async def _drain(gen) -> list:
    out = []
    async for item in gen:
        out.append(item)
    return out


def _use_fresh_table(monkeypatch) -> FakeDynamoResource:
    resource = FakeDynamoResource()
    monkeypatch.setattr(agent, "_dynamodb", resource)
    return resource


def test_entrypoint_routes_spec_to_spec_pipeline_success(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setattr(
        spec_agent, "run_spec_pipeline",
        lambda p: {"status": "success",
                   "pr_url": "https://github.com/o/r/pull/7",
                   "tasks": {"total": 2, "completed": ["1", "2"],
                             "blocked": None}},
    )

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items["ide-6f50f92a65f2"]
    assert item["status"] == "done"
    assert item["pr_url"] == "https://github.com/o/r/pull/7"


def test_entrypoint_routes_spec_blocked_to_tests_failed(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setattr(
        spec_agent, "run_spec_pipeline",
        lambda p: {"status": agent.STATUS_TESTS_FAILED,
                   "reason": f"blocked using {TOKEN_SENTINEL}",
                   "pr_url": "https://github.com/o/r/pull/8",
                   "draft": True,
                   "tasks": {"total": 3, "completed": ["1"],
                             "blocked": {"id": "2", "reason": "x"}}},
    )

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items["ide-6f50f92a65f2"]
    assert item["status"] == "tests_failed"
    # Reason redacted.
    assert TOKEN_SENTINEL not in item["reason"]
    assert agent.REDACTION_MASK in item["reason"]


def test_entrypoint_routes_spec_exception_to_failed(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)

    def boom(payload):
        raise RuntimeError(f"clone failed using {TOKEN_SENTINEL}")

    monkeypatch.setattr(spec_agent, "run_spec_pipeline", boom)

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items["ide-6f50f92a65f2"]
    assert item["status"] == "failed"
    assert TOKEN_SENTINEL not in item["reason"]
    assert agent.REDACTION_MASK in item["reason"]


def test_entrypoint_one_shot_still_uses_run_pipeline(monkeypatch):
    """A one_shot (or absent task_type) payload must NOT hit the spec path."""
    resource = _use_fresh_table(monkeypatch)
    called = {"one_shot": False, "spec": False}

    def fake_run_pipeline(p):
        called["one_shot"] = True
        return {"status": "success",
                "pr_url": "https://github.com/o/r/pull/1",
                "tests_passed": True, "attempts": 1}

    def fake_spec(p):
        called["spec"] = True
        return {"status": "success", "pr_url": "x", "tasks": {}}

    monkeypatch.setattr(agent, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(spec_agent, "run_spec_pipeline", fake_spec)

    one_shot_payload = {
        "ticket_id": "PROJ-15",
        "sonar_hash": "ide-oneshot",
        "repo_clone_url": "https://github.com/your-org/your-repo.git",
        "base_branch": "master",
        "fix_branch": "fix/x",
        "description": "do a thing",
        "summary": "x",
        "task_type": "one_shot",
    }
    asyncio.run(_drain(agent.entrypoint(one_shot_payload)))

    assert called["one_shot"] is True
    assert called["spec"] is False
    item = resource.Table(agent.DYNAMODB_TABLE).items["ide-oneshot"]
    assert item["status"] == "done"
