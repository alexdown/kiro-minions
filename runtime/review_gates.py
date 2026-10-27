"""Shared verification-gate module (ADR-008) for kiro-minions.

ADR-008 adds two post-build verification gates, both run OUTSIDE the coding
loop, as a bounded, deterministically-orchestrated review phase on the
CUMULATIVE change (the ``base..fix`` branch diff) before the PR is opened:

* **Gate A — adversarial review** of the cumulative diff + the job intent
  (summary/tasks.md for the spec-executor; ticket description/summary for the
  one-shot path). Verdict: ``APPROVED`` / ``CHANGES_REQUESTED``.
* **Gate B — verify-vs-spec**: does the cumulative diff satisfy the spec
  acceptance criteria (requirements.md / design.md)? Verdict:
  ``SATISFIES_SPEC`` / ``DOES_NOT_SATISFY``. When no acceptance source is
  available (e.g. the one-shot path has no Kiro spec) Gate B is SKIPPED with a
  notice, never blocking.

This is the SINGLE reusable core both worker paths call (``runtime/agent.py``
``run_pipeline`` for one-shot, ``runtime/spec_agent.py`` ``run_spec_pipeline``
for the spec-executor). It owns brief construction, the in-container kiro-cli
review invocation, a DEFENSIVE verdict parser, severity-gated draft/non-draft
routing, redaction, and the PR "Verification" section renderer. It contains NO
path-specific orchestration.

REUSE RULES (do not reinvent — mirror ``spec_agent.py``):
* Every subprocess goes through ``agent.RUNNER`` via ``agent._run`` with an argv
  LIST and ``shell=False``; the kiro-cli brief is delivered on STDIN
  (``input=agent.redact(brief)``), NEVER argv, invoked as
  ``['kiro-cli','chat','--no-interactive','--trust-all-tools']`` exactly like
  ``agent.step_kiro_fix`` / ``spec_agent._run_task_kiro``.
* ``agent.redact()`` masks secrets and wraps every brief/finding/verdict line.
* ``agent.truncate_test_output`` bounds the (possibly large) diff and intent.
* Per-gate flags + timeout are read at MODULE LEVEL (like
  ``spec_agent.SPEC_MAX_TASK_ATTEMPTS``) so a test ``importlib.reload`` picks up
  monkeypatched env.

FAIL-SAFE CONTRACT (ADR-008 Q4/Q5): the gates NEVER silently pass. An
unparseable / garbage / timed-out gate output is treated as INCONCLUSIVE, which
downgrades the PR to DRAFT with a notice stating the gate did not produce a
usable verdict. A PASSING ``APPROVED``/``SATISFIES_SPEC`` verdict with only
low-severity (or no) findings keeps the PR non-draft (low findings are
advisory); a FAILING verdict (``CHANGES_REQUESTED``/``DOES_NOT_SATISFY``)
downgrades to DRAFT on its own (the gate's top-level decision, ADR-008 Q4), as
does any high-severity finding even under a passing verdict (Q5). There is
deliberately NO ``SPEC_MAX_REVIEW_RETRIES`` and NO auto-remediation loop (Q4).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import agent  # reuse RUNNER/_run, redact, truncate_test_output, KIRO_TIMEOUT_S, log


log = agent.log

# ---------------------------------------------------------------------------
# Gate flags + per-gate timeout (read at MODULE LEVEL like
# spec_agent.SPEC_MAX_TASK_ATTEMPTS so a test importlib.reload picks up a
# monkeypatched env). Both gate flags default OFF: with the flags off the gate
# runners are no-ops (skipped) and both worker paths are byte-for-byte
# unchanged. There is NO SPEC_MAX_REVIEW_RETRIES (ADR-008 Q4: no
# auto-remediation loop).
# ---------------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    """Parse a boolean feature flag from the environment (default OFF).

    ``'1'`` / ``'true'`` / ``'yes'`` (case-insensitive, surrounding whitespace
    ignored) mean ON; everything else (including unset) means OFF.
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


# Gate A (adversarial review) enable flag.
SPEC_REVIEW_ENABLED = _env_flag("SPEC_REVIEW_ENABLED")

# Gate B (verify-vs-spec) enable flag.
SPEC_VERIFY_SPEC_ENABLED = _env_flag("SPEC_VERIFY_SPEC_ENABLED")

# Per-gate-pass timeout (seconds). A gate pass exceeding this raises
# subprocess.TimeoutExpired, which the gate runner catches and maps to an
# INCONCLUSIVE result (fail safe -> draft). Default 300 mirrors
# agent.KIRO_TIMEOUT_S.
SPEC_REVIEW_TIMEOUT_S = int(os.environ.get("SPEC_REVIEW_TIMEOUT_S", "300"))


# ---------------------------------------------------------------------------
# Verdict contract + gate kinds.
# ---------------------------------------------------------------------------

GATE_A = "gate_a"  # adversarial review
GATE_B = "gate_b"  # verify-vs-spec

GATE_LABELS = {GATE_A: "Gate A", GATE_B: "Gate B"}

# Allowed verdict values per gate kind.
VERDICT_APPROVED = "APPROVED"
VERDICT_CHANGES_REQUESTED = "CHANGES_REQUESTED"
VERDICT_SATISFIES_SPEC = "SATISFIES_SPEC"
VERDICT_DOES_NOT_SATISFY = "DOES_NOT_SATISFY"

# Sentinel verdict for a gate whose output could not be parsed into a known
# verdict (fail safe -> draft with notice).
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

# Per-kind sets: the first entry is the PASS verdict, the second the FAIL one.
_ALLOWED_VERDICTS = {
    GATE_A: (VERDICT_APPROVED, VERDICT_CHANGES_REQUESTED),
    GATE_B: (VERDICT_SATISFIES_SPEC, VERDICT_DOES_NOT_SATISFY),
}

# Severity values. Anything unknown/missing is coerced to "high" to fail safe.
SEVERITY_HIGH = "high"
SEVERITY_LOW = "low"


# Fenced ```json ... ``` block matcher (tolerant of a language tag or none).
_JSON_FENCE_RE = re.compile(
    r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class GateResult:
    """Normalized outcome of one gate pass.

    * ``kind`` — GATE_A / GATE_B.
    * ``verdict`` — one of the kind's allowed verdicts or VERDICT_INCONCLUSIVE.
    * ``findings`` — list of normalized ``{severity, file, line, message}`` dicts.
    * ``inconclusive`` — True when the output could not be parsed / the pass
      errored or timed out (fail safe).
    * ``skipped`` — True when the gate's flag is off or it has no input to run on.
    * ``skip_reason`` — human-readable reason when ``skipped``.
    * ``raw_error`` — a redacted short error string when a pass errored/timed out.
    """

    kind: str
    verdict: str = VERDICT_INCONCLUSIVE
    findings: list[dict] = field(default_factory=list)
    inconclusive: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    raw_error: str | None = None

    @property
    def label(self) -> str:
        return GATE_LABELS.get(self.kind, self.kind)

    @property
    def high_findings(self) -> list[dict]:
        return [f for f in self.findings if f.get("severity") == SEVERITY_HIGH]

    @property
    def is_failing_verdict(self) -> bool:
        """True when the verdict is the FAIL verdict for this kind."""
        allowed = _ALLOWED_VERDICTS.get(self.kind)
        return bool(allowed) and self.verdict == allowed[1]

    @property
    def is_pass_verdict(self) -> bool:
        allowed = _ALLOWED_VERDICTS.get(self.kind)
        return bool(allowed) and self.verdict == allowed[0]


# ---------------------------------------------------------------------------
# Defensive verdict parsing.
# ---------------------------------------------------------------------------


def _normalize_finding(raw: object) -> dict | None:
    """Coerce one raw finding into ``{severity, file, line, message}``.

    Non-dict findings are dropped (return None). An unknown/missing severity is
    coerced to ``high`` so an ambiguous finding fails SAFE (forces a draft).
    """
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity", "")).strip().lower()
    if severity not in {SEVERITY_HIGH, SEVERITY_LOW}:
        severity = SEVERITY_HIGH  # fail safe: treat unknown severity as high
    file_val = raw.get("file")
    file_str = str(file_val) if file_val is not None else None
    line_val = raw.get("line")
    if isinstance(line_val, bool):  # bool is an int subclass; reject it
        line = None
    elif isinstance(line_val, int):
        line = line_val
    else:
        line = None
    message = str(raw.get("message", "")).strip()
    return {
        "severity": severity,
        "file": file_str,
        "line": line,
        "message": message,
    }


def _normalize_findings(raw: object) -> list[dict]:
    """Coerce a raw findings value into a normalized list (drop non-dicts)."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        normalized = _normalize_finding(item)
        if normalized is not None:
            out.append(normalized)
    return out


def _extract_json_object(text: str) -> str | None:
    """Return the JSON object text to parse, or None if none can be found.

    Prefers the LAST fenced ```json block (tolerant of prose/markdown around
    it). Falls back to a bare top-level JSON object (the last ``{...}`` span)
    when no fence is present.
    """
    if not text:
        return None
    matches = _JSON_FENCE_RE.findall(text)
    if matches:
        return matches[-1].strip()
    # Fallback: a bare top-level object. Take the span from the FIRST '{' to the
    # LAST '}' so trailing prose is ignored; json.loads validates the rest.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1].strip()
    return None


def parse_verdict(text: str | None, kind: str) -> GateResult:
    """Parse a gate's kiro-cli output into a normalized GateResult.

    DEFENSIVE + FAIL SAFE: on ANY failure (no json found, JSONDecodeError, a
    non-object payload, or a verdict not in the kind's allowed set) this returns
    an INCONCLUSIVE result rather than raising. Findings are normalized (unknown
    severity -> high; non-dict findings dropped).
    """
    blob = _extract_json_object(text or "")
    if blob is None:
        return GateResult(
            kind=kind,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error="no JSON verdict block found in gate output",
        )
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return GateResult(
            kind=kind,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error="gate output JSON could not be decoded",
        )
    if not isinstance(parsed, dict):
        return GateResult(
            kind=kind,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error="gate verdict JSON was not an object",
        )

    verdict = parsed.get("verdict")
    allowed = _ALLOWED_VERDICTS.get(kind, ())
    if not isinstance(verdict, str) or verdict.strip().upper() not in allowed:
        # Unknown verdict: keep any findings but mark inconclusive (fail safe).
        return GateResult(
            kind=kind,
            verdict=VERDICT_INCONCLUSIVE,
            findings=_normalize_findings(parsed.get("findings")),
            inconclusive=True,
            raw_error=f"unknown verdict for {GATE_LABELS.get(kind, kind)}",
        )

    return GateResult(
        kind=kind,
        verdict=verdict.strip().upper(),
        findings=_normalize_findings(parsed.get("findings")),
        inconclusive=False,
    )


# ---------------------------------------------------------------------------
# Severity routing (ADR-008 Q5).
# ---------------------------------------------------------------------------


def _gate_drafts(result: GateResult) -> bool:
    """True when a single gate result downgrades the PR to draft.

    A gate contributes to DRAFT when:
      * it is inconclusive (fail safe); OR
      * its verdict is the FAIL verdict (CHANGES_REQUESTED / DOES_NOT_SATISFY)
        — the gate's top-level decision is "not acceptable", so it drafts
        regardless of the severity of any individual finding; OR
      * it has >= 1 high-severity finding (even under a PASS verdict — a
        high-severity finding is a block by itself per ADR-008 Q5).

    A skipped gate never drafts. A PASS verdict with only low-severity (or no)
    findings never drafts — those are advisory (ADR-008 Q5).

    ADR-008 reconciliation: Q4 routes a failing GATE VERDICT
    (CHANGES_REQUESTED / DOES_NOT_SATISFY) to a draft PR; Q5's severity gate
    layers on top of that, applying to the per-finding severity so that a
    high-severity finding also drafts even when the overall verdict passes,
    while low-severity findings stay advisory. A failing verdict is the gate's
    own decision that the change is not ready, so it drafts on its own.
    """
    if result.skipped:
        return False
    if result.inconclusive:
        return True
    if result.is_failing_verdict:
        return True
    if result.high_findings:
        return True
    return False


def decide_routing(results: list[GateResult]) -> tuple[bool, list[str]]:
    """Return ``(should_draft, failed_gate_labels)`` for a set of gate results.

    ``should_draft`` is True when ANY gate contributes to a draft (see
    ``_gate_drafts``). ``failed_gate_labels`` lists the human labels of the
    gates that contributed, in input order.
    """
    failed_labels: list[str] = []
    for result in results:
        if _gate_drafts(result):
            failed_labels.append(result.label)
    return (len(failed_labels) > 0, failed_labels)


# ---------------------------------------------------------------------------
# Brief construction.
# ---------------------------------------------------------------------------

_GATE_A_SCHEMA_EXAMPLE = (
    "```json\n"
    "{\n"
    '  "verdict": "APPROVED",\n'
    '  "findings": [\n'
    '    {"severity": "low", "file": "src/foo.py", "line": 42,\n'
    '     "message": "Consider a clearer variable name."}\n'
    "  ]\n"
    "}\n"
    "```"
)

_GATE_B_SCHEMA_EXAMPLE = (
    "```json\n"
    "{\n"
    '  "verdict": "SATISFIES_SPEC",\n'
    '  "findings": [\n'
    '    {"severity": "high", "file": "src/foo.py", "line": null,\n'
    '     "message": "Requirement 2.3 (rate limiting) is not implemented."}\n'
    "  ]\n"
    "}\n"
    "```"
)


def build_review_brief(diff: str | None, intent: str | None) -> str:
    """Compose the Gate A (adversarial review) brief (delivered via STDIN).

    The brief carries the bounded cumulative diff + the job intent and instructs
    the model to emit ONLY a fenced ```json verdict in the Gate A schema. The
    diff and intent are bounded via ``agent.truncate_test_output`` so an
    unbounded diff can never be placed in the brief. The caller routes the whole
    brief through ``agent.redact`` before it reaches kiro-cli.
    """
    diff_text = agent.truncate_test_output(diff or "")
    intent_text = agent.truncate_test_output(intent or "")
    return (
        "You are an ADVERSARIAL code reviewer with FRESH context. You see only "
        "the change (a unified diff) and the author's stated intent — not the "
        "author's reasoning. Review the change for bugs, security issues, "
        "missed edge cases, and sloppy or incorrect diffs.\n\n"
        "STATED INTENT:\n"
        f"{intent_text}\n\n"
        "CUMULATIVE DIFF UNDER REVIEW:\n"
        f"{diff_text}\n\n"
        "Respond with ONLY a single fenced json block (no prose before or "
        "after) matching EXACTLY this schema:\n"
        '  verdict: "APPROVED" when the change is safe to merge, or '
        '"CHANGES_REQUESTED" when it is not.\n'
        "  findings: a list of objects, each with:\n"
        '    severity: "high" (must block / downgrade to draft) or "low" '
        "(advisory only),\n"
        "    file: the repo-relative path (string) or null,\n"
        "    line: an integer line number or null,\n"
        "    message: a one-line description of the issue.\n"
        "Use an empty findings list when there is nothing to report. Example:\n"
        f"{_GATE_A_SCHEMA_EXAMPLE}\n"
    )


def build_verify_spec_brief(diff: str | None, acceptance_text: str | None) -> str:
    """Compose the Gate B (verify-vs-spec) brief (delivered via STDIN).

    The brief carries the bounded cumulative diff + the spec acceptance criteria
    (requirements.md / design.md text) and instructs the model to emit ONLY a
    fenced ```json verdict in the Gate B schema. Bounded + redacted like
    ``build_review_brief``.
    """
    diff_text = agent.truncate_test_output(diff or "")
    acceptance = agent.truncate_test_output(acceptance_text or "")
    return (
        "You are an INDEPENDENT verifier. Decide whether the change (a unified "
        "diff) SATISFIES the spec's acceptance criteria below — not merely "
        "whether tests pass. Map each unmet requirement to evidence.\n\n"
        "SPEC ACCEPTANCE CRITERIA (requirements / design):\n"
        f"{acceptance}\n\n"
        "CUMULATIVE DIFF UNDER REVIEW:\n"
        f"{diff_text}\n\n"
        "Respond with ONLY a single fenced json block (no prose before or "
        "after) matching EXACTLY this schema:\n"
        '  verdict: "SATISFIES_SPEC" when the change meets the acceptance '
        'criteria, or "DOES_NOT_SATISFY" when it does not.\n'
        "  findings: a list of objects, each with:\n"
        '    severity: "high" (unmet requirement / downgrade to draft) or '
        '"low" (advisory only),\n'
        "    file: the repo-relative path (string) or null,\n"
        "    line: an integer line number or null,\n"
        "    message: a one-line description mapping a requirement to evidence.\n"
        "Use an empty findings list when everything is satisfied. Example:\n"
        f"{_GATE_B_SCHEMA_EXAMPLE}\n"
    )


# ---------------------------------------------------------------------------
# Gate-pass runners.
# ---------------------------------------------------------------------------


def _run_gate_kiro(repo_dir: Path | str, brief: str) -> str:
    """Run ONE kiro-cli review pass, mirroring agent.step_kiro_fix exactly.

    The brief is delivered on STDIN (``input=``), never argv; the argv stays a
    LIST (shell=False); the timeout is the per-gate SPEC_REVIEW_TIMEOUT_S; the
    brief is routed through ``agent.redact`` so no secret can reach kiro-cli via
    stdin either. Returns the captured stdout. A subprocess.TimeoutExpired (or a
    RUNNER RuntimeError) propagates to the caller, which maps it to an
    INCONCLUSIVE result.
    """
    result = agent._run(
        ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"],
        cwd=repo_dir,
        timeout=SPEC_REVIEW_TIMEOUT_S,
        input=agent.redact(brief),
    )
    return result.stdout or ""


def run_gate_a(repo_dir: Path | str, diff: str | None,
               intent: str | None) -> GateResult:
    """Run Gate A (adversarial review). Flag-off -> skipped-with-reason.

    Fail safe: a TimeoutExpired or a RUNNER RuntimeError maps to an INCONCLUSIVE
    result with a redacted ``raw_error`` (which routes the PR to draft).
    """
    if not SPEC_REVIEW_ENABLED:
        return GateResult(kind=GATE_A, skipped=True,
                          skip_reason="flag disabled")
    brief = build_review_brief(diff, intent)
    try:
        stdout = _run_gate_kiro(repo_dir, brief)
    except subprocess.TimeoutExpired:
        return GateResult(
            kind=GATE_A,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error=agent.redact(
                f"Gate A kiro-cli timed out after {SPEC_REVIEW_TIMEOUT_S} seconds"
            ),
        )
    except RuntimeError as exc:
        return GateResult(
            kind=GATE_A,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error=agent.redact(f"Gate A review failed: {exc}"),
        )
    return parse_verdict(stdout, GATE_A)


def run_gate_b(repo_dir: Path | str, diff: str | None,
               acceptance_text: str | None) -> GateResult:
    """Run Gate B (verify-vs-spec). Flag-off -> skipped-with-reason.

    When no acceptance source is available (``acceptance_text`` empty), Gate B is
    SKIPPED with a notice (ADR-008 Q3: never block on a missing spec). Fail safe
    on timeout / RUNNER error -> INCONCLUSIVE (draft).
    """
    if not SPEC_VERIFY_SPEC_ENABLED:
        return GateResult(kind=GATE_B, skipped=True,
                          skip_reason="flag disabled")
    if not acceptance_text or not acceptance_text.strip():
        return GateResult(
            kind=GATE_B,
            skipped=True,
            skip_reason="no spec acceptance source available",
        )
    brief = build_verify_spec_brief(diff, acceptance_text)
    try:
        stdout = _run_gate_kiro(repo_dir, brief)
    except subprocess.TimeoutExpired:
        return GateResult(
            kind=GATE_B,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error=agent.redact(
                f"Gate B kiro-cli timed out after {SPEC_REVIEW_TIMEOUT_S} seconds"
            ),
        )
    except RuntimeError as exc:
        return GateResult(
            kind=GATE_B,
            verdict=VERDICT_INCONCLUSIVE,
            inconclusive=True,
            raw_error=agent.redact(f"Gate B verification failed: {exc}"),
        )
    return parse_verdict(stdout, GATE_B)


# ---------------------------------------------------------------------------
# PR "Verification" section renderer.
# ---------------------------------------------------------------------------


def _render_findings(findings: list[dict]) -> list[str]:
    """Render findings as severity-tagged markdown bullet lines."""
    lines: list[str] = []
    for f in findings:
        severity = (f.get("severity") or SEVERITY_HIGH).upper()
        location_bits = []
        if f.get("file"):
            loc = str(f["file"])
            if f.get("line") is not None:
                loc = f"{loc}:{f['line']}"
            location_bits.append(f"`{loc}`")
        location = (" " + " ".join(location_bits)) if location_bits else ""
        message = (f.get("message") or "").strip()
        lines.append(f"- **[{severity}]**{location} {message}".rstrip())
    return lines


def _render_gate_a(result: GateResult) -> list[str]:
    lines = ["### Gate A — adversarial review", ""]
    if result.skipped:
        lines += [f"_Skipped: {result.skip_reason}._", ""]
        return lines
    if result.inconclusive:
        lines += [
            ":warning: **INCONCLUSIVE** — the review did not produce a usable "
            "verdict, so this PR is opened as a **draft** for human review.",
            "",
        ]
        if result.raw_error:
            lines += [f"_Detail: {result.raw_error}_", ""]
    else:
        lines += [f"**Verdict:** `{result.verdict}`", ""]
    if result.findings:
        lines += ["**Findings:**", ""]
        lines += _render_findings(result.findings)
        lines += [""]
    return lines


def _render_gate_b(result: GateResult) -> list[str]:
    lines = ["### Gate B — verify-vs-spec", ""]
    if result.skipped:
        lines += [
            f":information_source: **Spec verification did not run:** "
            f"{result.skip_reason}.",
            "",
        ]
        return lines
    if result.inconclusive:
        lines += [
            ":warning: **INCONCLUSIVE** — spec verification did not produce a "
            "usable verdict, so this PR is opened as a **draft** for human "
            "review.",
            "",
        ]
        if result.raw_error:
            lines += [f"_Detail: {result.raw_error}_", ""]
    else:
        lines += [f"**Verdict:** `{result.verdict}`", ""]
    if result.findings:
        lines += ["**Findings:**", ""]
        lines += _render_findings(result.findings)
        lines += [""]
    return lines


def render_verification_section(gate_a_result: GateResult,
                                gate_b_result: GateResult) -> str:
    """Render the PR-body "Verification" section for both gate results.

    Pure function returning a markdown string so both PR-body builders can embed
    it. When a gate caused the draft, the section names WHICH gate(s). All text
    is safe to pass through ``redact()`` by the caller (findings/verdicts come
    from model text that the caller redacts).
    """
    should_draft, failed_labels = decide_routing([gate_a_result, gate_b_result])

    lines = ["## Verification", ""]
    if should_draft:
        lines += [
            "> :warning: **VERIFICATION GATE(S) FLAGGED — DRAFT PR FOR HUMAN "
            "REVIEW**",
            "",
            "The following gate(s) downgraded this PR to a draft: "
            + ", ".join(f"**{label}**" for label in failed_labels)
            + ".",
            "",
        ]
    else:
        lines += ["All enabled verification gates passed (advisory findings, "
                  "if any, are listed below).", ""]

    lines += _render_gate_a(gate_a_result)
    lines += _render_gate_b(gate_b_result)
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Single high-level entry both callers use.
# ---------------------------------------------------------------------------


@dataclass
class GatePhaseResult:
    """Bundled outcome of the whole post-build gate phase.

    ``should_draft`` and ``failed_gate_labels`` drive the PR mode;
    ``verification_section`` is the ready-to-embed markdown (the caller routes it
    through ``redact()`` with the rest of the PR body).
    """

    gate_a_result: GateResult
    gate_b_result: GateResult
    should_draft: bool
    failed_gate_labels: list[str]
    verification_section: str

    @property
    def any_gate_ran(self) -> bool:
        """True when at least one gate actually executed (not skipped)."""
        return not (self.gate_a_result.skipped and self.gate_b_result.skipped)


def run_gates(repo_dir: Path | str, diff: str | None, intent: str | None,
              acceptance_text: str | None = None) -> GatePhaseResult:
    """Run both gates and bundle the routing + rendered verification section.

    This is the SINGLE function ``run_pipeline`` (one-shot) and
    ``run_spec_pipeline`` (spec-executor) call, so neither path duplicates gate
    orchestration. With both flags off both gates are skipped, ``should_draft``
    is False, and the rendered section simply notes the gates did not run — so
    the caller's existing draft/non-draft decision is untouched (fail safe +
    flags-off = unchanged behaviour).
    """
    gate_a_result = run_gate_a(repo_dir, diff, intent)
    gate_b_result = run_gate_b(repo_dir, diff, acceptance_text)
    should_draft, failed_labels = decide_routing(
        [gate_a_result, gate_b_result]
    )
    section = render_verification_section(gate_a_result, gate_b_result)
    return GatePhaseResult(
        gate_a_result=gate_a_result,
        gate_b_result=gate_b_result,
        should_draft=should_draft,
        failed_gate_labels=failed_labels,
        verification_section=section,
    )
