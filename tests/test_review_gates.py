"""FEAT-001 tests for the shared verification-gate module (runtime/review_gates.py).

The gate module is exercised directly against a FakeRunner injected into
``agent.RUNNER`` (no real kiro-cli). Module-level flags are read at import, so
each flags-on test monkeypatches the env and ``importlib.reload(review_gates)``
so the override takes effect. The module imports ``agent`` and reuses
agent.RUNNER/_run, agent.redact, agent.truncate_test_output — it defines no new
subprocess runner.

Coverage:
  * parse_verdict defensive paths (clean, garbage, bare object, prose-wrapped);
  * severity routing (clean -> not draft, high -> draft, low -> advisory,
    inconclusive -> draft);
  * run_gate_a / run_gate_b against FakeRunner incl. timeout -> inconclusive,
    flag off -> skipped (no kiro call), Gate B empty acceptance -> skip notice;
  * secret hygiene: sentinel masked, brief via STDIN (CallRecord.input) not
    argv, kiro argv is a LIST (not shell string).
"""

from __future__ import annotations

import importlib
import subprocess

import pytest

import agent  # conftest installs SDK stubs first
import review_gates
from fakes import FakeRunner, ScriptedResult


TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"


@pytest.fixture(autouse=True)
def restore_runner():
    original = agent.RUNNER
    yield
    agent.RUNNER = original
    # Test isolation: ``_reload_with_flags`` reloads review_gates with the gate
    # flags ON, which mutates the module-level ``SPEC_REVIEW_ENABLED`` /
    # ``SPEC_VERIFY_SPEC_ENABLED``. monkeypatch restores the ENV at test end but
    # NOT the already-reloaded module constants; a later test that reads those
    # constants (e.g. the one-shot run_pipeline gate phase, FEAT-003) would then
    # see them stuck ON. Reload once more here with the (now-restored) clean env
    # so the module returns to its flags-OFF default, mirroring the teardown in
    # tests/test_spec_gates.py.
    importlib.reload(review_gates)


def _reload_with_flags(monkeypatch, *, gate_a=False, gate_b=False,
                       timeout=None):
    """Reload review_gates with the given flag env so module-level flags apply."""
    monkeypatch.setenv("SPEC_REVIEW_ENABLED", "1" if gate_a else "0")
    monkeypatch.setenv("SPEC_VERIFY_SPEC_ENABLED", "1" if gate_b else "0")
    if timeout is not None:
        monkeypatch.setenv("SPEC_REVIEW_TIMEOUT_S", str(timeout))
    return importlib.reload(review_gates)


# ---------------------------------------------------------------------------
# No SPEC_MAX_REVIEW_RETRIES (ADR-008 Q4).
# ---------------------------------------------------------------------------


def test_no_review_retries_var_anywhere():
    assert not hasattr(review_gates, "SPEC_MAX_REVIEW_RETRIES")


# ---------------------------------------------------------------------------
# parse_verdict defensive parsing.
# ---------------------------------------------------------------------------


def test_parse_verdict_clean_gate_a_approved():
    text = '```json\n{"verdict": "APPROVED", "findings": []}\n```'
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    assert res.verdict == review_gates.VERDICT_APPROVED
    assert res.inconclusive is False
    assert res.findings == []


def test_parse_verdict_messy_prose_around_fence_still_parses():
    text = (
        "Sure, here is my review of the change.\n\n"
        "Some analysis prose here.\n"
        "```json\n{\"verdict\": \"CHANGES_REQUESTED\", \"findings\": "
        "[{\"severity\": \"high\", \"file\": \"a.py\", \"line\": 3, "
        "\"message\": \"bug\"}]}\n```\n"
        "Hope that helps!"
    )
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    assert res.verdict == review_gates.VERDICT_CHANGES_REQUESTED
    assert res.inconclusive is False
    assert len(res.findings) == 1
    assert res.findings[0]["severity"] == "high"


def test_parse_verdict_bare_object_without_fence():
    text = 'prefix {"verdict": "SATISFIES_SPEC", "findings": []} suffix'
    res = review_gates.parse_verdict(text, review_gates.GATE_B)
    assert res.verdict == review_gates.VERDICT_SATISFIES_SPEC
    assert res.inconclusive is False


def test_parse_verdict_last_fence_wins():
    text = (
        '```json\n{"verdict": "APPROVED", "findings": []}\n```\n'
        '```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
        '[{"severity": "high", "message": "x"}]}\n```'
    )
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    assert res.verdict == review_gates.VERDICT_CHANGES_REQUESTED


def test_parse_verdict_garbage_is_inconclusive():
    res = review_gates.parse_verdict("no json here at all", review_gates.GATE_A)
    assert res.inconclusive is True
    assert res.verdict == review_gates.VERDICT_INCONCLUSIVE
    assert res.raw_error


def test_parse_verdict_bad_json_is_inconclusive():
    text = "```json\n{verdict: oops not valid json,}\n```"
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    assert res.inconclusive is True


def test_parse_verdict_unknown_verdict_is_inconclusive():
    text = '```json\n{"verdict": "MAYBE", "findings": []}\n```'
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    assert res.inconclusive is True


def test_parse_verdict_unknown_severity_coerced_high():
    text = (
        '```json\n{"verdict": "APPROVED", "findings": '
        '[{"severity": "medium", "message": "x"}]}\n```'
    )
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    # Unknown severity coerced to high (fail safe).
    assert res.findings[0]["severity"] == "high"


def test_parse_verdict_drops_non_dict_findings():
    text = (
        '```json\n{"verdict": "APPROVED", "findings": '
        '["not a dict", {"severity": "low", "message": "ok"}]}\n```'
    )
    res = review_gates.parse_verdict(text, review_gates.GATE_A)
    assert len(res.findings) == 1
    assert res.findings[0]["severity"] == "low"


# ---------------------------------------------------------------------------
# Severity routing.
# ---------------------------------------------------------------------------


def test_routing_clean_approved_not_draft():
    res = review_gates.parse_verdict(
        '```json\n{"verdict": "APPROVED", "findings": []}\n```',
        review_gates.GATE_A,
    )
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is False
    assert labels == []


def test_routing_high_finding_drafts_and_labels_gate_a():
    res = review_gates.parse_verdict(
        '```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
        '[{"severity": "high", "message": "x"}]}\n```',
        review_gates.GATE_A,
    )
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is True
    assert "Gate A" in labels


def test_routing_low_only_is_advisory_not_draft():
    res = review_gates.parse_verdict(
        '```json\n{"verdict": "APPROVED", "findings": '
        '[{"severity": "low", "message": "nit"}]}\n```',
        review_gates.GATE_A,
    )
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is False
    assert labels == []


def test_routing_inconclusive_drafts():
    res = review_gates.parse_verdict("garbage", review_gates.GATE_A)
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is True
    assert "Gate A" in labels


def test_routing_changes_requested_zero_findings_drafts():
    # ADR-008 Q4: a FAIL verdict is the gate's top-level "not acceptable"
    # decision, so it drafts even with NO findings at all.
    res = review_gates.parse_verdict(
        '```json\n{"verdict": "CHANGES_REQUESTED", "findings": []}\n```',
        review_gates.GATE_A,
    )
    assert res.is_failing_verdict is True
    assert res.high_findings == []
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is True
    assert "Gate A" in labels


def test_routing_changes_requested_low_only_findings_drafts():
    # A CHANGES_REQUESTED verdict whose findings are all LOW severity still
    # drafts: the failing verdict itself routes to draft regardless of the
    # per-finding severity (ADR-008 Q4). Low severity stays advisory only when
    # the VERDICT passes (see test_routing_low_only_is_advisory_not_draft).
    res = review_gates.parse_verdict(
        '```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
        '[{"severity": "low", "message": "style nit"}]}\n```',
        review_gates.GATE_A,
    )
    assert res.is_failing_verdict is True
    assert res.high_findings == []
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is True
    assert "Gate A" in labels


def test_routing_does_not_satisfy_low_only_findings_drafts():
    # Same for Gate B: DOES_NOT_SATISFY with only low findings drafts.
    res = review_gates.parse_verdict(
        '```json\n{"verdict": "DOES_NOT_SATISFY", "findings": '
        '[{"severity": "low", "message": "minor gap"}]}\n```',
        review_gates.GATE_B,
    )
    assert res.is_failing_verdict is True
    assert res.high_findings == []
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is True
    assert "Gate B" in labels


def test_routing_skipped_never_drafts():
    res = review_gates.GateResult(
        kind=review_gates.GATE_B, skipped=True, skip_reason="flag disabled")
    should_draft, labels = review_gates.decide_routing([res])
    assert should_draft is False
    assert labels == []


# ---------------------------------------------------------------------------
# run_gate_a / run_gate_b against FakeRunner.
# ---------------------------------------------------------------------------


def test_run_gate_a_flag_off_skipped_no_kiro_call(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=False)
    runner = FakeRunner(results=[])
    agent.RUNNER = runner

    res = rg.run_gate_a("/repo", "diff", "intent")
    assert res.skipped is True
    assert res.skip_reason == "flag disabled"
    assert runner.count("kiro-cli chat") == 0


def test_run_gate_a_clean_approved_not_draft(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=True)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "APPROVED", "findings": []}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_a("/repo", "the diff", "the intent")
    assert res.verdict == rg.VERDICT_APPROVED
    should_draft, _ = rg.decide_routing([res])
    assert should_draft is False
    assert runner.count("kiro-cli chat") == 1


def test_run_gate_a_high_finding_drafts(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=True)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
                   '[{"severity": "high", "file": "a.py", "line": 1, '
                   '"message": "sql injection"}]}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_a("/repo", "the diff", "the intent")
    should_draft, labels = rg.decide_routing([res])
    assert should_draft is True
    assert "Gate A" in labels


def test_run_gate_a_garbage_inconclusive_drafts(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=True)
    runner = FakeRunner(results=[
        ScriptedResult(stdout="I could not review this.", match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_a("/repo", "the diff", "the intent")
    assert res.inconclusive is True
    should_draft, _ = rg.decide_routing([res])
    assert should_draft is True


def test_run_gate_a_timeout_inconclusive_drafts(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=True, timeout=5)
    timeout_exc = subprocess.TimeoutExpired(cmd="kiro-cli", timeout=5)
    runner = FakeRunner(results=[
        ScriptedResult(raises=timeout_exc, match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_a("/repo", "the diff", "the intent")
    assert res.inconclusive is True
    assert res.raw_error
    should_draft, _ = rg.decide_routing([res])
    assert should_draft is True


def test_run_gate_a_runner_error_inconclusive(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=True)
    # rc != 0 with check=True makes FakeRunner raise RuntimeError like the real
    # runner; the gate maps it to inconclusive (fail safe).
    runner = FakeRunner(results=[
        ScriptedResult(returncode=2, stderr="boom", match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_a("/repo", "the diff", "the intent")
    assert res.inconclusive is True
    assert res.raw_error


def test_run_gate_b_satisfies_not_draft(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_b=True)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "SATISFIES_SPEC", "findings": []}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_b("/repo", "the diff", "acceptance criteria text")
    assert res.verdict == rg.VERDICT_SATISFIES_SPEC
    should_draft, _ = rg.decide_routing([res])
    assert should_draft is False


def test_run_gate_b_does_not_satisfy_high_drafts(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_b=True)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "DOES_NOT_SATISFY", "findings": '
                   '[{"severity": "high", "message": "req 2 unmet"}]}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    res = rg.run_gate_b("/repo", "the diff", "acceptance criteria text")
    should_draft, labels = rg.decide_routing([res])
    assert should_draft is True
    assert "Gate B" in labels


def test_run_gate_b_flag_on_but_no_acceptance_skips_with_notice(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_b=True)
    runner = FakeRunner(results=[])
    agent.RUNNER = runner

    res = rg.run_gate_b("/repo", "the diff", "")
    assert res.skipped is True
    assert res.skip_reason == "no spec acceptance source available"
    assert runner.count("kiro-cli chat") == 0


def test_run_gate_b_flag_off_skipped(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_b=False)
    runner = FakeRunner(results=[])
    agent.RUNNER = runner

    res = rg.run_gate_b("/repo", "the diff", "acceptance text")
    assert res.skipped is True
    assert res.skip_reason == "flag disabled"
    assert runner.count("kiro-cli chat") == 0


# ---------------------------------------------------------------------------
# run_gates high-level entry.
# ---------------------------------------------------------------------------


def test_run_gates_both_off_not_draft_and_notes_skips(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=False, gate_b=False)
    runner = FakeRunner(results=[])
    agent.RUNNER = runner

    phase = rg.run_gates("/repo", "diff", "intent", "acceptance")
    assert phase.should_draft is False
    assert phase.any_gate_ran is False
    assert runner.count("kiro-cli chat") == 0
    assert "## Verification" in phase.verification_section


def test_run_gates_gate_a_high_names_gate_in_section(monkeypatch):
    rg = _reload_with_flags(monkeypatch, gate_a=True, gate_b=False)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
                   '[{"severity": "high", "file": "a.py", "line": 7, '
                   '"message": "leaks a secret"}]}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    phase = rg.run_gates("/repo", "diff", "intent", None)
    assert phase.should_draft is True
    assert "Gate A" in phase.failed_gate_labels
    assert "Gate A" in phase.verification_section
    assert "[HIGH]" in phase.verification_section


def test_run_gates_gate_a_changes_requested_low_only_drafts(monkeypatch):
    # End-to-end pin for the ADR-008 Q4 edge: a CHANGES_REQUESTED verdict with
    # only LOW findings still downgrades the PR to draft and names Gate A in the
    # rendered section (the verdict itself is the draft driver).
    rg = _reload_with_flags(monkeypatch, gate_a=True, gate_b=False)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
                   '[{"severity": "low", "file": "a.py", "line": 2, '
                   '"message": "rename this variable"}]}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    phase = rg.run_gates("/repo", "diff", "intent", None)
    assert phase.should_draft is True
    assert "Gate A" in phase.failed_gate_labels
    assert "FLAGGED" in phase.verification_section
    assert "CHANGES_REQUESTED" in phase.verification_section


# ---------------------------------------------------------------------------
# Secret hygiene.
# ---------------------------------------------------------------------------


def test_secret_masked_and_brief_via_stdin_argv_is_list(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    rg = _reload_with_flags(monkeypatch, gate_a=True)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "APPROVED", "findings": []}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    # Put the sentinel in both the diff and the intent.
    diff = f"diff with {TOKEN_SENTINEL} inside"
    intent = f"intent mentioning {TOKEN_SENTINEL}"
    rg.run_gate_a("/repo", diff, intent)

    call = runner.find("kiro-cli chat")
    assert call is not None
    # argv is a LIST (not a shell string), shell=False invariant preserved.
    assert isinstance(call.cmd, list)
    assert call.cmd == ["kiro-cli", "chat", "--no-interactive",
                        "--trust-all-tools"]
    # Brief is delivered via STDIN (input=), NOT in argv.
    assert call.input is not None
    assert TOKEN_SENTINEL not in " ".join(call.cmd)
    # The sentinel is masked wherever the brief text is handed to kiro.
    assert TOKEN_SENTINEL not in call.input
    assert agent.REDACTION_MASK in call.input
    # And the full recorded surface (every argv + every stdin) has no unmasked
    # sentinel.
    for rec in runner.calls:
        argv = rec.cmd if isinstance(rec.cmd, list) else [rec.cmd]
        assert TOKEN_SENTINEL not in " ".join(str(a) for a in argv)
        if rec.input is not None:
            assert TOKEN_SENTINEL not in rec.input


def test_verification_section_with_secret_findings_redactable(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    rg = _reload_with_flags(monkeypatch, gate_a=True)
    runner = FakeRunner(results=[
        ScriptedResult(
            stdout='```json\n{"verdict": "CHANGES_REQUESTED", "findings": '
                   f'[{{"severity": "high", "message": "token {TOKEN_SENTINEL}"}}]'
                   '}\n```',
            match="kiro-cli chat"),
    ])
    agent.RUNNER = runner

    phase = rg.run_gates("/repo", "diff", "intent", None)
    # The caller routes the section through redact(); after redaction no
    # sentinel remains.
    redacted = agent.redact(phase.verification_section)
    assert TOKEN_SENTINEL not in redacted
