"""Tests for the runtime coarse-phase emission (FEAT-003 / ADR-003).

Covers runtime/agent.py:

* ``write_phase`` updates ONLY the ``phase`` attribute on the status row (never
  touches ``status``) and is best-effort (a write failure is swallowed).
* ``run_pipeline`` emits the expected phase transitions in order
  (cloning -> fixing -> testing -> opening_pr) via best-effort writes, without
  changing the terminal ``status`` write behaviour.
* The entrypoint still writes the terminal status unchanged, and additionally
  records a terminal phase mapped from that status.

Subprocess calls route through ``FakeRunner`` so nothing real runs; phase writes
are captured by monkeypatching ``agent.write_phase``. SDK stubs are installed by
tests/conftest.py before these imports resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest

import agent  # conftest installs SDK stubs first
from fakes import FakeDynamoResource, FakeRunner, ScriptedResult


SONAR_HASH = "6f50f92a-65f2-4c4c-aae5-9b069b44430a"


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
        "sonar_hash": SONAR_HASH,
    }
    p.update(overrides)
    return p


@pytest.fixture
def fixed_workdir(monkeypatch):
    """A real tmp workdir with a tests/ marker so pytest detection is stable."""
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
    original = agent.RUNNER
    yield
    agent.RUNNER = original


def _capture_phases(monkeypatch) -> list[tuple[str, str]]:
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent, "write_phase",
        lambda sonar_hash, phase: recorded.append((sonar_hash, phase)),
    )
    return recorded


# ---------------------------------------------------------------------------
# write_phase unit behaviour
# ---------------------------------------------------------------------------


def test_write_phase_updates_only_phase(monkeypatch):
    resource = FakeDynamoResource()
    monkeypatch.setattr(agent, "_dynamodb", resource)
    table = resource.Table(agent.DYNAMODB_TABLE)
    # Seed a row that already carries a terminal-ish status to prove it is kept.
    table.items[SONAR_HASH] = {"sonar_hash": SONAR_HASH, "status": "in_flight"}

    agent.write_phase(SONAR_HASH, agent.PHASE_CLONING)

    item = table.items[SONAR_HASH]
    assert item["phase"] == "cloning"
    # The status attribute is untouched by a phase write.
    assert item["status"] == "in_flight"
    assert "updated_at" in item


def test_write_phase_missing_sonar_hash_noop(monkeypatch):
    resource = FakeDynamoResource()
    monkeypatch.setattr(agent, "_dynamodb", resource)
    # Must not raise and must not write anything.
    agent.write_phase("", agent.PHASE_CLONING)
    assert resource.Table(agent.DYNAMODB_TABLE).items == {}


def test_write_phase_best_effort_swallows_errors(monkeypatch):
    class ExplodingResource:
        def Table(self, name):  # noqa: N802 - boto3 API name
            raise RuntimeError("dynamo down")

    monkeypatch.setattr(agent, "_dynamodb", ExplodingResource())
    # Must not raise.
    agent.write_phase(SONAR_HASH, agent.PHASE_TESTING)


# ---------------------------------------------------------------------------
# run_pipeline emits ordered phase transitions
# ---------------------------------------------------------------------------


def test_run_pipeline_emits_ordered_phases_happy_path(fixed_workdir, monkeypatch):
    recorded = _capture_phases(monkeypatch)
    pr_url = "https://github.com/your-org/your-repo/pull/42"
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

    phases = [phase for (sh, phase) in recorded]
    assert phases == [
        agent.PHASE_CLONING,
        agent.PHASE_FIXING,
        agent.PHASE_TESTING,
        agent.PHASE_OPENING_PR,
    ]
    # Every phase write is keyed by the job's sonar_hash.
    assert all(sh == SONAR_HASH for (sh, _phase) in recorded)


def test_run_pipeline_emits_testing_each_attempt(fixed_workdir, monkeypatch):
    """A retry loop emits the testing phase once per verified attempt."""
    recorded = _capture_phases(monkeypatch)
    runner = FakeRunner(
        results=[
            ScriptedResult(returncode=1, stdout="baseline failed", match="pytest"),
            ScriptedResult(returncode=1, stdout="1 failed", match="pytest"),
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

    phases = [phase for (sh, phase) in recorded]
    # cloning + fixing once, testing once per attempt (2), then opening_pr.
    assert phases == [
        agent.PHASE_CLONING,
        agent.PHASE_FIXING,
        agent.PHASE_TESTING,
        agent.PHASE_TESTING,
        agent.PHASE_OPENING_PR,
    ]


def test_run_pipeline_no_diff_stops_before_opening_pr(fixed_workdir, monkeypatch):
    """When kiro produces no diff, opening_pr is never emitted."""
    recorded = _capture_phases(monkeypatch)
    runner = FakeRunner(
        results=[ScriptedResult(returncode=0, match="git diff --cached --quiet")]
    )
    agent.RUNNER = runner

    result = agent.run_pipeline(_payload())
    assert result["status"] == "failed"

    phases = [phase for (sh, phase) in recorded]
    assert agent.PHASE_OPENING_PR not in phases
    # The earlier phases still fired.
    assert agent.PHASE_CLONING in phases
    assert agent.PHASE_FIXING in phases


# ---------------------------------------------------------------------------
# Entrypoint still writes the terminal status + a terminal phase
# ---------------------------------------------------------------------------


async def _drain(gen) -> list:
    out = []
    async for item in gen:
        out.append(item)
    return out


def test_entrypoint_terminal_status_unchanged_adds_phase(monkeypatch):
    resource = FakeDynamoResource()
    monkeypatch.setattr(agent, "_dynamodb", resource)
    monkeypatch.setattr(
        agent, "run_pipeline",
        lambda p: {"status": "success",
                   "pr_url": "https://github.com/o/r/pull/7",
                   "tests_passed": True, "attempts": 1},
    )

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    # Terminal status write behaviour is unchanged.
    assert item["status"] == "done"
    assert item["pr_url"] == "https://github.com/o/r/pull/7"
    # Additive: a terminal phase mapped from the status.
    assert item["phase"] == agent.PHASE_FOR_STATUS["done"]


def test_entrypoint_failed_sets_terminal_phase(monkeypatch):
    resource = FakeDynamoResource()
    monkeypatch.setattr(agent, "_dynamodb", resource)

    def boom(payload):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(agent, "run_pipeline", boom)

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == "failed"
    assert item["phase"] == agent.PHASE_FOR_STATUS["failed"]
