# ADR-008: Verification gates in the spec-executor (adversarial review + verify-vs-spec)

**Status:** PROPOSED (needs your decisions)
**Context doc:** [../verification-gates.md](../verification-gates.md)
**Depends on:** [ADR-002](ADR-002-task-shape.md) (spec-executor exists),
[ADR-003](ADR-003-results-channel.md), [ADR-005](ADR-005-payload-naming.md),
[ADR-007](ADR-007-concurrency-and-cost-guardrails.md)

## Context

The spec-executor's only per-task acceptance gate is "the repo's tests pass"
(see [../spec-executor.md](../spec-executor.md) §5). That implements BUILD +
a thin deterministic VERIFY from the AI-SDLC lifecycle pattern, but skips the two
gates that make a change *trustworthy* before a human reviews it:

- **adversarial REVIEW** (fresh context) of the implementation, and
- **VERIFY-VS-SPEC** — does the change satisfy the spec's acceptance criteria,
  not just the tests?

We want to add both **inside the worker, before the PR**, without breaking the
project's **B1** principle (deterministic outer loop; model only inside bounded
steps). These gates are model-in-a-loop activities, so this ADR must pin down how
they stay bounded and deterministically orchestrated, plus several product
choices about engine, granularity, inputs, failure routing, and rollout.

## Decision (framework — options per question below)

**Committed regardless of the per-question choices:** both gates are **bounded**
(fixed pass/retry budgets, never open-ended) and **orchestrated by deterministic
code** (the outer loop decides when a gate runs, what it is fed, and how a
pass/fail routes; the model only produces the review/verdict inside each bounded
pass). This preserves B1. Everything else is a decision for you:

### Q1 — Review engine
- **Option 1 (recommended):** in-container `kiro-cli` review invocation — reuses
  auth/redaction/RUNNER, no new infra; we own the verdict format.
- **Option 2:** sub-agent / separate reviewer process (stronger "independent"
  story; more moving parts in the image).
- **Option 3:** a different/stronger model for review than for build (best
  independence; extra cost + a second model dependency).

### Q2 — Review granularity
- **Per task (recommended):** Gate A runs after each task's tests pass, before
  that task's commit — catches issues before they compound; more model calls.
- **Per spec:** one review of the whole diff at the end — cheaper, coarser,
  harder to attribute a finding to a task.

### Q3 — Spec inputs & Gate B availability
- Extend the payload/ingress with `requirements_md`/`design_md` (repo-relative
  path, re-validated + clone-root-contained, like `tasks_md_path`; or inline).
- If no spec acceptance source is present: **skip-with-notice (recommended)** —
  Gate B is skipped, the PR says spec-verification did not run — vs **block**
  (refuse to open a non-draft PR without a spec to verify against).

### Q4 — Verify-vs-spec failure routing
- **Draft-PR-to-human (recommended):** a `DOES_NOT_SATISFY` verdict opens a draft
  PR flagged as a spec-conformance failure with findings; a human (or a fresh
  planning session) re-plans. Fits the no-autonomous-planner-in-container
  reality.
- **Autonomous re-plan loop:** the worker loops back to re-derive tasks and
  re-run — much larger scope, reintroduces a planning loop; only if you want it.

### Q5 — Blocking vs advisory review findings (Gate A)
- **Blocking:** `CHANGES_REQUESTED` sends the task back to BUILD (bounded by
  `SPEC_MAX_REVIEW_RETRIES`); exhaustion = blocker.
- **Advisory:** findings are recorded in the PR but never stop a task.
- **Severity-gated (recommended):** high-severity blocks, low-severity advisory.

### Q6 — Scope
- **Spec-executor only (recommended v1).** The one-shot minion stays unchanged.
- **Also wire Gate A into the one-shot path** (reuse later; larger blast radius).

### Q7 — Budgets & rollout flags
- `SPEC_MAX_REVIEW_RETRIES` default (e.g. `2`).
- Ship behind `SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED` flags —
  **default off (recommended)** for a safe rollout, flip on per deployment — vs
  default on.

## Consequences

- **More model calls per job** → cost and latency rise; ADR-007 budgets
  (`SPEC_JOB_TIMEOUT_S`, per-actor quota, concurrency cap) matter more, and new
  `SPEC_MAX_REVIEW_RETRIES` + feature flags are added. Default-off flags make the
  gates opt-in until proven.
- **Gate B needs the spec in the worker** → a payload/ingress change (Q3) and a
  default `.kiro/specs/<feature>/` sibling-lookup convention.
- **Stronger pre-human trust**: the human Gate 2 now reviews a change that has
  passed tests + adversarial review + (where available) spec conformance; draft
  PRs clearly state which gate failed.
- **B1 preserved** as long as the committed boundedness/determinism constraints
  hold; the ADR explicitly forbids an open-ended review loop.
- **Verdict contract**: whichever engine (Q1), the gates need a stable,
  machine-parseable verdict schema (status + findings) the deterministic loop can
  route on — to be defined in the implementing spec/PR.

## Status / next step

PROPOSED. Record your choices for Q1–Q7 (edit this ADR to ACCEPTED with the
selected options + a one-line rationale each), then a fresh session implements
the gates on top of `runtime/spec_agent.py` per [../verification-gates.md](../verification-gates.md).
