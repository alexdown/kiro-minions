# Spec: verification gates for the spec-executor (adversarial review + verify-vs-spec)

**Status:** DRAFT v1 — PROPOSAL for review. No code. Specs only.
**Decision record:** [adr/ADR-008-verification-gates.md](adr/ADR-008-verification-gates.md) (PROPOSED — needs your decisions)
**Builds on:** [spec-executor.md](spec-executor.md), [adr/ADR-002-task-shape.md](adr/ADR-002-task-shape.md)
**Inspiration:** the "AI Software Development Lifecycle" pattern — `BUILD → VERIFY → REVIEW → VERIFY-VS-SPEC → human gate`, with review failures looping back to the builder and spec failures escalating to the planner.

---

## 1. Why

The spec-executor today (see [spec-executor.md](spec-executor.md) §3) has exactly
**one** verification gate per task: **the repo's existing tests pass.** In the
lifecycle diagram's terms it implements **BUILD (04)** + a thin **VERIFY (05 ·
deterministic · no model)**, and then opens the PR. It does **not** do:

- **REVIEW (06, fresh context):** an adversarial read of the implementation to
  catch bugs, security issues, missed edge cases, sloppy diffs — things tests
  don't cover.
- **VERIFY-VS-SPEC (07, independent):** a check that the change actually
  satisfies the spec / acceptance criteria, not merely that tests are green.

"Tests pass" is a weak proxy for "correct and matches intent." A task can go
green while implementing the wrong thing, leaving a security hole, or satisfying
only the tests the model itself wrote. This spec proposes adding the two missing
gates **inside the worker, before the PR is opened (for the happy path) / before
a task is accepted** — while staying faithful to the project's B1 principle.

---

## 2. The B1 tension (read this before deciding)

kiro-minions is deliberately **B1**: a *deterministic outer loop*, with model
reasoning only *inside* bounded steps. Today the only model call in the
spec-executor is the per-task `kiro-cli` coding pass.

Adversarial review and verify-vs-spec are **model-in-a-loop** activities. Adding
them means **more model invocations** in the worker. That is compatible with B1
**only if** they are:

1. **bounded** — a fixed number of review/verify passes, never an open-ended
   "keep reviewing until happy" loop; and
2. **orchestrated by deterministic code** — the outer loop decides when to run a
   gate, what to feed it, and how to route a pass/fail; the model only produces
   the review/verdict inside each bounded pass.

If we keep both properties, the gates are "more bounded model steps inside a
deterministic loop," not "an outer agent loop." That is the design constraint
this spec commits to. **ADR-008 records it.**

---

## 3. Proposed gate placement

Two gates, at two different granularities:

```
spec-executor per-task loop (extended):

  FOR each task:
     BUILD    kiro-cli coding pass (fresh context)           [existing]
     VERIFY   run repo tests (deterministic, no model)        [existing]
       │ tests fail → retry (existing per-task budget)
       ▼ tests pass
     REVIEW   adversarial review of THIS task's diff          [NEW - gate A]
              (fresh context, bounded)
       │ findings (blocking) → back to BUILD (same task,
       │                        bounded review-retry budget)
       ▼ clean (or advisory-only)
     COMMIT   one commit for this task                        [existing]

  after ALL tasks complete (happy path only):
     VERIFY-VS-SPEC  independent check of the cumulative       [NEW - gate B]
                     diff vs requirements/design/tasks
       │ pass  → open NON-draft PR                 (human = Gate 2)
       ▼ fail  → open DRAFT PR flagged
                 "spec-conformance failure" + findings  (human gate / re-plan)
```

### Gate A — adversarial review (per task, after tests pass, before commit)
- **Input:** this task's diff (`git diff` of the staged/working changes for the
  task), the task's brief (title + body + requirement refs), and the minimal
  spec context already assembled for the coding pass. **Fresh context** — the
  reviewer does not see the coding pass's transcript, only the artifact (the
  diff) and the intent (the task). This is the "06 REVIEW · fresh context" box.
- **Output:** a structured verdict — `APPROVED` / `CHANGES_REQUESTED` — plus a
  list of findings (severity + file/line + rationale).
- **Routing:** `CHANGES_REQUESTED` (blocking findings) → back to **BUILD** for
  the *same task*, feeding the findings into the next coding brief, bounded by a
  new `SPEC_MAX_REVIEW_RETRIES` budget. Exhausting it = a **blocker**
  (stop-at-first-blocker, draft PR, as today). `APPROVED` (or advisory-only
  findings under the chosen policy) → proceed to commit.

### Gate B — verify-vs-spec (once per spec, after all tasks, before the PR)
- **Input:** the cumulative branch diff (base..fix branch), plus the **spec
  acceptance criteria** — i.e. `requirements.md` and/or `design.md`, not just
  `tasks.md`. (This requires the worker to have the spec; see §5 — a payload/
  ingress change.) This is the "07 VERIFY VS SPEC · independent" box.
- **Output:** a structured verdict — `SATISFIES_SPEC` / `DOES_NOT_SATISFY` +
  findings mapping unmet requirements to evidence.
- **Routing:** `SATISFIES_SPEC` → open the normal **non-draft** PR; the human is
  Gate 2 ("ship it on green gates"). `DOES_NOT_SATISFY` → open a **draft** PR
  flagged as a spec-conformance failure with the findings. We have **no
  autonomous planner in the container**, so the diagram's "fail → back to the
  planner" becomes a **human gate** by default (the human / a fresh planning
  session re-plans). An autonomous re-plan loop is a much larger, separate option
  (ADR-008 Q4).

---

## 4. Who performs a gate? (the review engine)

Three ways to run the bounded review/verify pass inside the container:

- **Option 1 — in-container `kiro-cli` review invocation.** The worker calls
  `kiro-cli` again with a review brief (the diff + the intent) and parses a
  structured verdict. Pros: no new infra, reuses the auth/redaction/RUNNER
  machinery already in the worker; same cost model as the coding pass. Cons: we
  define and parse the verdict format ourselves; "independence" is only as strong
  as a fresh invocation (new context, same underlying model).
- **Option 2 — a sub-agent / separate reviewer process.** Mirror the
  orchestrator's own `semantic_reviewer` pattern: a distinct agent with its own
  prompt and fresh context produces the review. Pros: stronger "independent
  reviewer" story; reusable verdict contract. Cons: more moving parts inside the
  container; needs the sub-agent runtime available in the image.
- **Option 3 — a different model for review than for build.** Use a stronger/
  different model for the gates than for the coding pass (defence against a model
  rubber-stamping its own work). Pros: best independence. Cons: cost, and a
  second model dependency in the container.

All three keep the outer loop deterministic; they differ in independence
strength and cost. See ADR-008 Q1.

---

## 5. Spec inputs the worker needs (new)

Gate B checks against the **acceptance criteria**, which live in
`requirements.md` / `design.md`, not `tasks.md`. Today the payload carries only
`tasks_md` / `tasks_md_path` ([ide-ingress.md](ide-ingress.md) §5). To do Gate B
we must give the worker the spec. Proposed, consistent with the existing
`tasks_md_path` approach:

- Add optional `requirements_md_path` / `design_md_path` (repo-relative, read
  after clone, traversal-validated and clone-root-contained exactly like
  `tasks_md_path`), and/or inline `requirements_md` / `design_md`.
- Default convention: if `tasks_md_path` is `.kiro/specs/<feature>/tasks.md`,
  the worker looks for `requirements.md` / `design.md` as siblings unless
  overridden. (A Kiro spec directory already groups the three files.)
- If no spec acceptance source is available, **Gate B degrades gracefully**: it
  is skipped and the PR body says spec-verification was not run (so we never
  *block* on a missing spec, but we're honest that the gate didn't run). See
  ADR-008 Q3.

Gate A needs no new inputs (it works from the task's diff + the task brief it
already has).

---

## 6. Status, PR body, and budgets

- **Phases (ADR-003):** extend the per-task phase beyond `task N/total` to name
  the gate in flight, e.g. `task 7/20: reviewing` and a spec-level
  `verifying-spec`, so the polling API shows where a long job is.
- **PR body:** add a "Verification" section — per-task review outcomes (and any
  advisory findings carried forward) and the spec-verification verdict. A draft
  PR caused by a gate failure states WHICH gate failed and the findings
  (redacted).
- **Budgets (ADR-007 family):** new, env-configurable like the existing
  `SPEC_*` vars:
  - `SPEC_MAX_REVIEW_RETRIES` (default TBD, e.g. `2`) — max BUILD↔REVIEW cycles
    per task before a review failure becomes a blocker.
  - `SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED` — feature flags so the
    gates can be rolled out / disabled per deployment.
  - review/verify passes count toward the per-task and per-job model-call cost;
    `SPEC_JOB_TIMEOUT_S` still bounds overall wall-clock.
- **Secret safety:** every review/verify brief, finding, verdict, and PR line is
  routed through `redact()` and delivered on STDIN, exactly like the coding
  brief. No diff or finding may leak a secret into argv/logs/PR.

---

## 7. What this is NOT (scope guards)

- Not an open-ended agent loop — both gates are bounded (§2).
- Not a replacement for the human gate — the human still approves the PR
  ("Gate 2 · ship it on green gates"); these gates make the PR *trustworthy*
  before it reaches the human.
- Not a re-plan engine by default — a spec-conformance failure escalates to a
  human/draft PR; an autonomous "back to the planner" loop is an explicit,
  larger option (ADR-008 Q4), out of scope unless you choose it.
- Not applied to the one-shot minion — this is a spec-executor enhancement; the
  `task_type="one_shot"` path is unchanged (though Gate A *could* be reused there
  later; ADR-008 Q6).

---

## 8. Decisions I need from you

Recorded in [adr/ADR-008-verification-gates.md](adr/ADR-008-verification-gates.md):

- **Q1 — Review engine:** in-container `kiro-cli` (Option 1), sub-agent reviewer
  (Option 2), or a different/stronger review model (Option 3)?
- **Q2 — Review granularity:** Gate A **per task** (tighter, catches issues
  before they compound, more model calls) vs **once per spec** at the end
  (cheaper, coarser)? (Recommendation: per task.)
- **Q3 — Spec inputs & Gate B:** OK to extend the payload/ingress with
  `requirements_md`/`design_md` (path or inline)? And if the spec is absent,
  skip-with-notice (recommended) vs block?
- **Q4 — Verify-vs-spec failure routing:** draft-PR-to-human (recommended; fits
  no-planner-in-container) vs an autonomous re-plan loop (much larger)?
- **Q5 — Blocking vs advisory:** are Gate A findings **blocking** (must fix to
  proceed) or **advisory** (recorded in the PR, don't stop the task)? Possibly
  severity-gated (e.g. high = block, low = advisory).
- **Q6 — Scope:** spec-executor only (recommended v1), or also wire Gate A into
  the one-shot minion?
- **Q7 — Budgets/flags defaults:** `SPEC_MAX_REVIEW_RETRIES` default; ship the
  gates behind `*_ENABLED` flags defaulting off (safe rollout) or on?

Nothing is built yet. Once you record the decisions in ADR-008, a fresh session
implements the gates on top of the spec-executor.
