"""FEAT-002 tests: the spec-executor Phase-2 verification gates (ADR-008).

These exercise the POST-LOOP gate phase wired into
``runtime/spec_agent.run_spec_pipeline``. The gates are a single bounded phase
that runs ONCE, after ALL tasks are built/tested/committed and after the push,
but BEFORE the PR is opened, on the CUMULATIVE ``base..fix`` branch diff.

Flags default OFF; with the flags off the phase is a no-op and the existing
spec-executor behaviour (tests/test_spec_agent.py) is byte-for-byte unchanged.
These tests flip the flags ON by monkeypatching the env and
``importlib.reload(review_gates)`` so the module-level flags pick up the
override, then reload ``spec_agent`` so it re-imports the reloaded gate module.

Harness contract (see FEAT-001 findings): the ScriptedResult-based FakeRunner
(``agent.RUNNER``) has NO ``contains()`` / ``flat_text()`` helpers (those live on
RecordingRunner); we iterate ``runner.calls`` and inspect ``CallRecord.cmd`` /
``.input`` directly. The cumulative diff is scripted via
``ScriptedResult(match='git diff master..')`` and the kiro review verdicts via
``ScriptedResult(match='kiro-cli chat')`` (ordered FIFO among matchers).
"""

from __future__ import annotations

import contextlib
import importlib
import tempfile
from pathlib import Path

import pytest

import agent  # conftest installs SDK stubs first
import review_gates
import spec_agent
from fakes import FakeRunner, ScriptedResult


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"
KEY_SENTINEL = "kiro-key-SENTINEL-987654321"

THREE_TASKS = """# Implementation Plan

- [ ] 1. First task
  - do the first thing
- [ ] 2. Second task
  - do the second thing
- [ ] 3. Third task
  - do the third thing
"""

DIFF_TEXT = "diff --git a/a.py b/a.py\n+print('hello')\n"

GATE_A_CLEAN = (
    "Here is my review.\n"
    "```json\n"
    '{"verdict": "APPROVED", "findings": []}\n'
    "```\n"
)
GATE_A_HIGH = (
    "```json\n"
    '{"verdict": "CHANGES_REQUESTED", "findings": ['
    '{"severity": "high", "file": "a.py", "line": 1, '
    '"message": "SQL injection risk"}]}\n'
    "```\n"
)
GATE_B_SATISFIES = (
    "```json\n"
    '{"verdict": "SATISFIES_SPEC", "findings": []}\n'
    "```\n"
)
GATE_B_FAILS = (
    "```json\n"
    '{"verdict": "DOES_NOT_SATISFY", "findings": ['
    '{"severity": "high", "file": null, "line": null, '
    '"message": "Requirement 2.3 not implemented"}]}\n'
    "```\n"
)


@pytest.fixture(autouse=True)
def restore_runner():
    original = agent.RUNNER
    yield
    agent.RUNNER = original


@pytest.fixture
def gates_on(monkeypatch):
    """Turn BOTH gate flags ON and reload the gate module + spec_agent.

    Yields the reloaded ``spec_agent`` module (whose ``review_gates`` reference
    now points at the reloaded, flags-ON gate module). Teardown restores the
    env and reloads both modules back to their flags-OFF module-level state so
    other tests are unaffected.
    """
    monkeypatch.setenv("SPEC_REVIEW_ENABLED", "1")
    monkeypatch.setenv("SPEC_VERIFY_SPEC_ENABLED", "1")
    importlib.reload(review_gates)
    reloaded = importlib.reload(spec_agent)
    try:
        assert review_gates.SPEC_REVIEW_ENABLED is True
        assert review_gates.SPEC_VERIFY_SPEC_ENABLED is True
        yield reloaded
    finally:
        monkeypatch.delenv("SPEC_REVIEW_ENABLED", raising=False)
        monkeypatch.delenv("SPEC_VERIFY_SPEC_ENABLED", raising=False)
        importlib.reload(review_gates)
        importlib.reload(spec_agent)


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


def _make_workdir(mod, monkeypatch, *, markers=("tests",),
                  spec_files: dict[str, str] | None = None) -> Path:
    """Create a fake temp workdir rooted at a ``your-repo`` clone dir.

    ``spec_files`` optionally writes {relative_path: content} files into the
    repo (used to seed on-disk requirements.md / design.md / tasks.md).
    ``mod`` is the (possibly reloaded) spec_agent module to patch.
    """
    base = Path(tempfile.mkdtemp(prefix="kmgate-"))
    workdir = base / "work"
    workdir.mkdir()
    repo_dir = workdir / "your-repo"
    repo_dir.mkdir(parents=True)
    for marker in markers:
        (repo_dir / marker).mkdir(parents=True, exist_ok=True)
    for rel, content in (spec_files or {}).items():
        dest = repo_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    @contextlib.contextmanager
    def _fake_tempdir(*args, **kwargs):
        yield str(workdir)

    monkeypatch.setattr(agent.tempfile, "TemporaryDirectory", _fake_tempdir)
    monkeypatch.setattr(mod.tempfile, "TemporaryDirectory", _fake_tempdir)
    return workdir


def _commit_cycle(status_after: str) -> list[ScriptedResult]:
    return [
        ScriptedResult(stdout=status_after, match="git status --porcelain"),
        ScriptedResult(returncode=1, match="git diff --cached --quiet"),
    ]


def _three_tasks_pass_results(review_verdicts: list[str],
                              pr_url: str = "https://github.com/o/r/pull/1"
                              ) -> list[ScriptedResult]:
    """Build the FakeRunner script for 3 tasks that all pass, then the gate
    phase. ``review_verdicts`` are the kiro STDOUTs for the gate review passes
    (Gate A then, if present, Gate B). The 3 per-task CODING kiro passes and the
    gate review passes ALL display 'kiro-cli chat' identically, so they are
    scripted as ONE ordered FIFO matcher queue: 3 empty coding passes first,
    then the review verdicts in order."""
    kiro_matchers = [
        # The 3 per-task coding passes (no verdict output; tests pass via pytest).
        ScriptedResult(stdout="", match="kiro-cli chat"),
        ScriptedResult(stdout="", match="kiro-cli chat"),
        ScriptedResult(stdout="", match="kiro-cli chat"),
    ]
    for verdict in review_verdicts:
        kiro_matchers.append(ScriptedResult(stdout=verdict, match="kiro-cli chat"))
    return [
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
        # Cumulative diff for the gate phase.
        ScriptedResult(stdout=DIFF_TEXT, match="git diff master.."),
        # All kiro passes (coding + review), in call order.
        *kiro_matchers,
        ScriptedResult(stdout=pr_url, match="gh pr create"),
    ]


def _kiro_review_calls(runner: FakeRunner) -> list:
    """kiro-cli calls whose STDIN brief is a review/verify brief (NOT a per-task
    coding brief). Review briefs contain the gate schema language."""
    out = []
    for c in runner.calls:
        if "kiro-cli chat" in c.display and c.input and (
            "ADVERSARIAL code reviewer" in c.input
            or "INDEPENDENT verifier" in c.input
        ):
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# (a) both gates clean -> NON-draft PR with a Verification section
# ---------------------------------------------------------------------------


def test_both_gates_clean_non_draft_with_verification_section(gates_on,
                                                              monkeypatch):
    mod = gates_on
    _make_workdir(mod, monkeypatch)
    runner = FakeRunner(results=_three_tasks_pass_results(
        [GATE_A_CLEAN, GATE_B_SATISFIES],
        pr_url="https://github.com/o/r/pull/42",
    ))
    agent.RUNNER = runner

    # Provide an inline spec so Gate B has acceptance text to verify against.
    result = mod.run_spec_pipeline(_payload(
        requirements_md="Requirement 1: it must greet the user.",
    ))

    assert result["status"] == "success"
    assert result["draft"] is False
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" in body
    assert "APPROVED" in body
    assert "SATISFIES_SPEC" in body
    # Exactly two review passes (Gate A + Gate B) on top of the 3 coding passes.
    assert len(_kiro_review_calls(runner)) == 2
    assert runner.count("kiro-cli chat") == 5


# ---------------------------------------------------------------------------
# (b) Gate A high finding -> DRAFT PR flagged 'Gate A'
# ---------------------------------------------------------------------------


def test_gate_a_high_finding_drafts_pr(gates_on, monkeypatch):
    mod = gates_on
    _make_workdir(mod, monkeypatch)
    runner = FakeRunner(results=_three_tasks_pass_results(
        [GATE_A_HIGH, GATE_B_SATISFIES],
        pr_url="https://github.com/o/r/pull/9",
    ))
    agent.RUNNER = runner

    result = mod.run_spec_pipeline(_payload(
        requirements_md="Requirement 1: greet the user.",
    ))

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert "Gate A" in result["reason"]
    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" in body
    assert "Gate A" in body
    assert "SQL injection risk" in body
    # Commits were still kept/pushed (ADR-008 Q4): 3 task commits + a push.
    assert runner.count("git commit") == 3
    assert runner.find("git push") is not None


# ---------------------------------------------------------------------------
# (c) Gate B DOES_NOT_SATISFY high -> DRAFT PR flagged 'Gate B'
# ---------------------------------------------------------------------------


def test_gate_b_does_not_satisfy_drafts_pr(gates_on, monkeypatch):
    mod = gates_on
    _make_workdir(mod, monkeypatch)
    runner = FakeRunner(results=_three_tasks_pass_results(
        [GATE_A_CLEAN, GATE_B_FAILS],
        pr_url="https://github.com/o/r/pull/10",
    ))
    agent.RUNNER = runner

    result = mod.run_spec_pipeline(_payload(
        design_md="Design: the system rate-limits requests.",
    ))

    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert "Gate B" in result["reason"]
    assert "Gate A" not in result["reason"]
    gh = runner.find("gh pr create")
    assert "--draft" in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "DOES_NOT_SATISFY" in body
    assert "Requirement 2.3 not implemented" in body


# ---------------------------------------------------------------------------
# (d) Gate B spec absent -> skip-with-notice + NON-draft when Gate A clean
# ---------------------------------------------------------------------------


def test_gate_b_absent_spec_skips_with_notice_non_draft(gates_on, monkeypatch):
    mod = gates_on
    _make_workdir(mod, monkeypatch)
    runner = FakeRunner(results=_three_tasks_pass_results(
        [GATE_A_CLEAN],  # Gate B is skipped (no spec), so no verify pass.
        pr_url="https://github.com/o/r/pull/11",
    ))
    agent.RUNNER = runner

    # No inline spec, no *_md_path, and tasks come inline (no .kiro sibling) ->
    # Gate B has nothing to verify against.
    result = mod.run_spec_pipeline(_payload())

    assert result["status"] == "success"
    assert result["draft"] is False
    gh = runner.find("gh pr create")
    assert "--draft" not in gh.cmd
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" in body
    assert "APPROVED" in body
    assert "Spec verification did not run" in body
    # Only Gate A actually invoked kiro (Gate B skipped before any call).
    assert len(_kiro_review_calls(runner)) == 1


def test_gate_b_sibling_default_from_tasks_md_path(gates_on, monkeypatch):
    """requirements.md next to a .kiro/specs/<feature>/tasks.md is found."""
    mod = gates_on
    _make_workdir(mod, monkeypatch, spec_files={
        ".kiro/specs/feature/tasks.md": "- [ ] 1. First task\n",
        ".kiro/specs/feature/requirements.md": "Requirement 1: greet.",
    })
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        ScriptedResult(stdout=DIFF_TEXT, match="git diff master.."),
        # 1 coding pass, then Gate A + Gate B review passes (FIFO on display).
        ScriptedResult(stdout="", match="kiro-cli chat"),
        ScriptedResult(stdout=GATE_A_CLEAN, match="kiro-cli chat"),
        ScriptedResult(stdout=GATE_B_SATISFIES, match="kiro-cli chat"),
        ScriptedResult(stdout="https://github.com/o/r/pull/12",
                       match="gh pr create"),
    ])
    agent.RUNNER = runner

    payload = _payload()
    del payload["tasks_md"]
    payload["tasks_md_path"] = ".kiro/specs/feature/tasks.md"

    result = mod.run_spec_pipeline(payload)
    assert result["status"] == "success"
    assert result["draft"] is False
    # Gate B actually ran (found the sibling requirements.md) -> two review passes.
    assert len(_kiro_review_calls(runner)) == 2
    verify = [c for c in _kiro_review_calls(runner)
              if "INDEPENDENT verifier" in c.input][0]
    assert "Requirement 1: greet." in verify.input


# ---------------------------------------------------------------------------
# (e) Phase-1 blocker -> gates DO NOT run; blocker draft-PR behaviour preserved
# ---------------------------------------------------------------------------


def test_phase1_blocker_skips_gates(gates_on, monkeypatch):
    mod = gates_on
    _make_workdir(mod, monkeypatch)
    fail = "FAILED tests/test_x.py::test_a\nAssertionError: broken"
    runner = FakeRunner(results=[
        ScriptedResult(stdout="", match="git status --porcelain"),
        # Task 1 passes + commits.
        ScriptedResult(returncode=0, stdout="ok", match="pytest"),
        *_commit_cycle(" M a.py\n"),
        ScriptedResult(stdout=" M a.py\n", match="git status --porcelain"),
        # Task 2 fails every attempt -> blocker; task 3 never attempted.
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(returncode=1, stdout=fail, match="pytest"),
        ScriptedResult(stdout="https://github.com/o/r/pull/13",
                       match="gh pr create"),
    ])
    agent.RUNNER = runner

    result = mod.run_spec_pipeline(_payload(
        requirements_md="Requirement 1: greet.",
    ))

    # Today's blocker behaviour preserved exactly.
    assert result["status"] == agent.STATUS_TESTS_FAILED
    assert result["draft"] is True
    assert result["tasks"]["completed"] == ["1"]
    assert result["tasks"]["blocked"]["id"] == "2"
    # Gates did NOT run: no review kiro call, no cumulative diff, no gate phase.
    assert len(_kiro_review_calls(runner)) == 0
    assert runner.find("git diff master..") is None
    # The PR body has NO Verification section on the blocker path.
    gh = runner.find("gh pr create")
    body = gh.cmd[gh.cmd.index("--body") + 1]
    assert "## Verification" not in body


# ---------------------------------------------------------------------------
# (f) requirements_md_path read after clone works; traversal rejected
# ---------------------------------------------------------------------------


def test_requirements_md_path_read_after_clone(gates_on, monkeypatch):
    mod = gates_on
    _make_workdir(mod, monkeypatch, spec_files={
        "docs/requirements.md": "Requirement 1: greet the user clearly.",
    })
    runner = FakeRunner(results=_three_tasks_pass_results(
        [GATE_A_CLEAN, GATE_B_SATISFIES],
        pr_url="https://github.com/o/r/pull/14",
    ))
    agent.RUNNER = runner

    result = mod.run_spec_pipeline(_payload(
        requirements_md_path="docs/requirements.md",
    ))
    assert result["status"] == "success"
    assert result["draft"] is False
    verify = [c for c in _kiro_review_calls(runner)
              if "INDEPENDENT verifier" in c.input][0]
    assert "Requirement 1: greet the user clearly." in verify.input


@pytest.mark.parametrize("bad_path", [
    "../../etc/passwd",
    "/etc/passwd",
    "a/../../b",
    "-rf",
])
def test_requirements_md_path_traversal_rejected(bad_path):
    repo = Path(tempfile.mkdtemp(prefix="kmgate-trav-"))
    with pytest.raises(ValueError):
        spec_agent._resolve_contained_file(
            repo, bad_path, field="requirements_md_path",
        )


def test_read_spec_acceptance_absent_returns_none(monkeypatch):
    repo = Path(tempfile.mkdtemp(prefix="kmgate-none-"))
    out = spec_agent._read_spec_acceptance(_payload(), repo, None)
    assert out is None


# ---------------------------------------------------------------------------
# Secret hygiene with gates ON
# ---------------------------------------------------------------------------


def test_no_secret_in_gate_argv_stdin_or_pr_body(gates_on, monkeypatch):
    mod = gates_on
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("KIRO_API_KEY", KEY_SENTINEL)
    _make_workdir(mod, monkeypatch)
    gate_a_leaky = (
        "```json\n"
        '{"verdict": "CHANGES_REQUESTED", "findings": ['
        '{"severity": "high", "file": "a.py", "line": 1, '
        f'"message": "leaked {TOKEN_SENTINEL}"}}]}}\n'
        "```\n"
    )
    runner = FakeRunner(results=_three_tasks_pass_results(
        [gate_a_leaky, GATE_B_SATISFIES],
        pr_url="https://github.com/o/r/pull/15",
    ))
    agent.RUNNER = runner

    result = mod.run_spec_pipeline(_payload(
        summary=f"Feature using {KEY_SENTINEL}",
        requirements_md=f"Requirement uses {TOKEN_SENTINEL}",
    ))
    assert result["draft"] is True  # high finding -> draft

    for call in runner.calls:
        parts = call.cmd if isinstance(call.cmd, list) else [call.cmd]
        blob = "\n".join(str(p) for p in parts)
        if call.input:
            blob += "\n" + call.input
        assert TOKEN_SENTINEL not in blob
        assert KEY_SENTINEL not in blob
    assert TOKEN_SENTINEL not in result["reason"]


# ---------------------------------------------------------------------------
# Flags-OFF regression: NO extra kiro review call; PR stays non-draft
# ---------------------------------------------------------------------------


def test_flags_off_no_review_call_non_draft(monkeypatch):
    """With flags OFF (the default) the gate phase is a no-op: no review kiro
    pass beyond the 3 coding passes, and the PR stays non-draft."""
    # Ensure a clean, flags-OFF module state regardless of prior test order.
    monkeypatch.delenv("SPEC_REVIEW_ENABLED", raising=False)
    monkeypatch.delenv("SPEC_VERIFY_SPEC_ENABLED", raising=False)
    importlib.reload(review_gates)
    mod = importlib.reload(spec_agent)
    try:
        assert review_gates.SPEC_REVIEW_ENABLED is False
        assert review_gates.SPEC_VERIFY_SPEC_ENABLED is False
        _make_workdir(mod, monkeypatch)
        runner = FakeRunner(results=_three_tasks_pass_results(
            [],  # flags off: no review verdicts expected.
            pr_url="https://github.com/o/r/pull/16",
        ))
        agent.RUNNER = runner

        result = mod.run_spec_pipeline(_payload(
            requirements_md="Requirement 1: greet.",
        ))

        assert result["status"] == "success"
        assert result["draft"] is False
        # Exactly 3 kiro passes (the coding passes) — no review pass.
        assert runner.count("kiro-cli chat") == 3
        assert len(_kiro_review_calls(runner)) == 0
        gh = runner.find("gh pr create")
        assert "--draft" not in gh.cmd
        body = gh.cmd[gh.cmd.index("--body") + 1]
        assert "## Verification" not in body
    finally:
        importlib.reload(review_gates)
        importlib.reload(spec_agent)
