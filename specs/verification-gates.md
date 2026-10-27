# Spec: verification gates for the spec-executor (adversarial review + verify-vs-spec)

**Status:** ACCEPTED — implementation in progress. (Gate placement is post-loop,
once per task group; see §3. Decisions Q1–Q7 recorded in the ADR, with Q6
overridden to apply to BOTH worker paths.)
**Decision record:** [adr/ADR-008-verification-gates.md](adr/ADR-008-verification-gates.md) (ACCEPTED)
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
gates as a **post-loop review phase in the worker** — run once, after the whole
task group is built, tested, and committed, and before the PR is opened (§3) —
while staying faithful to the project's B1 principle.

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

If we keep both properties, the gates are "a bounded, code-orchestrated review
phase after a deterministic loop," not "an outer agent loop." That is the design
constraint this spec commits to. **ADR-008 records it.** (Placement — outside the
loop, once per group — is fixed in §3; boundedness is the constraint here.)

---

## 3. Gate placement: a post-loop review phase (maintainer-directed)

Both gates run **OUTSIDE the coding loop**, as a single **post-loop review
phase** that executes **once per task group (spec)** — after every task has been
built, tested, and committed. They are NOT interleaved per task.

This keeps the coding loop (Phase 1) purely BUILD + deterministic test-VERIFY
with no model-based gate inside it, and isolates all gate reasoning in one
clearly bounded Phase 2 that runs on the **cumulative branch diff**. It is the
cleaner B1 story (the loop stays deterministic end to end; the gates are a
separate, bounded, code-orchestrated phase) and it reviews the whole change as
one coherent unit rather than task-by-task fragments — so cross-task
inconsistencies are visible.

```
PHASE 1 — BUILD (coding loop, UNCHANGED from spec-executor.md §3):
  FOR each task:
     BUILD   kiro-cli coding pass (fresh context)            [existing]
     VERIFY  run repo tests (deterministic, no model)         [existing]
       tests fail → per-task retry budget ; still failing → blocker (stop)
     COMMIT  one commit for this task                         [existing]
  (stop-at-first-blocker still applies; a blocker ends Phase 1 early)

PHASE 2 — VERIFY GATES (NEW, runs ONCE, only if Phase 1 completed ALL tasks):
     Gate A  adversarial review of the CUMULATIVE branch diff (fresh context)
     Gate B  verify-vs-spec: cumulative diff vs requirements/design acceptance
     → combine verdicts, then open ONE PR:
          both pass (or advisory-only) → NON-draft PR         (human = Gate 2)
          a gate fails (per Q5)        → DRAFT PR flagged with which gate(s)
                                          failed + findings   (human gate)
```

**If Phase 1 stops at a blocker** (not all tasks done), the gates do **not** run
— there is no complete change to verify — and the draft PR is opened for the
blocker exactly as today (see [spec-executor.md](spec-executor.md) §5).

### Gate A — adversarial review (post-loop, on the cumulative diff)
- **Input:** the cumulative branch diff (`base..fix` branch), the job `summary`,
  and the `tasks.md` (intent). **Fresh context** — the reviewer sees only the
  artifact (the diff) and the intent, never the coding passes' transcripts. This
  is the "06 REVIEW · fresh context" box, applied to the whole change.
- **Output:** a structured verdict — `APPROVED` / `CHANGES_REQUESTED` — plus
  findings (severity + file/line + rationale).

### Gate B — verify-vs-spec (post-loop, on the cumulative diff)
- **Input:** the cumulative branch diff, plus the **spec acceptance criteria** —
  `requirements.md` and/or `design.md`, not just `tasks.md` (requires the worker
  to have the spec; see §5 — a payload/ingress change). This is the "07 VERIFY
  VS SPEC · independent" box.
- **Output:** a structured verdict — `SATISFIES_SPEC` / `DOES_NOT_SATISFY` +
  findings mapping unmet requirements to evidence.

### Combined routing (post-loop)
The deterministic phase combines both verdicts and decides the PR mode:
- **both pass** (or only advisory findings, per Q5) → open a **non-draft** PR;
  the human is Gate 2 ("ship it on green gates").
- **either fails** (per Q5's blocking policy) → open a **draft** PR flagged with
  which gate(s) failed + the findings. The task commits are kept and pushed
  regardless, so no work is lost. We have **no autonomous planner in the
  container**, so the diagram's "fail → back to the planner" is a **human gate**
  by default; an optional bounded auto-remediation pass is ADR-008 Q4.

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

Gate A needs no new inputs (it works from the cumulative branch diff + the
`tasks.md`/`summary` already present).

---

## 6. Status, PR body, and budgets

- **Phases (ADR-003):** after the per-task `task N/total` phases of Phase 1, the
  post-loop phase writes gate phases, e.g. `reviewing` (Gate A) and
  `verifying-spec` (Gate B), so the polling API shows where a long job is.
- **PR body:** add a "Verification" section — the Gate A review verdict + any
  advisory findings, and the Gate B spec-verification verdict. A draft PR caused
  by a gate failure states WHICH gate(s) failed and the findings (redacted).
- **Budgets (ADR-007 family):** new, env-configurable like the existing
  `SPEC_*` vars:
  - `SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED` — feature flags so each
    gate can be rolled out / disabled per deployment (ADR-008 Q7).
  - `SPEC_REVIEW_TIMEOUT_S` — per gate-pass timeout; gate passes count toward the
    overall `SPEC_JOB_TIMEOUT_S` wall-clock budget.
  - `SPEC_MAX_REVIEW_RETRIES` — **only exists if** ADR-008 Q4 chooses the
    optional bounded auto-remediation path (max post-loop BUILD→re-gate cycles
    before downgrading to a draft PR). With the recommended draft-PR-to-human
    routing there is no review-retry loop and this var is not needed.
- **Secret safety:** every review/verify brief, finding, verdict, and PR line is
  routed through `redact()` and delivered on STDIN, exactly like the coding
  brief. No diff or finding may leak a secret into argv/logs/PR.

---

## 7. What this is NOT (scope guards)

- Not inside the coding loop — both gates run **once, post-loop**, on the
  cumulative diff (§3); the Phase 1 coding loop is unchanged.
- Not an open-ended agent loop — the gate phase is bounded (§2): a fixed set of
  gate passes, no "review until happy."
- Not a replacement for the human gate — the human still approves the PR
  ("Gate 2 · ship it on green gates"); these gates make the PR *trustworthy*
  before it reaches the human.
- Not a re-plan engine by default — a gate failure opens a draft PR for a human;
  an autonomous bounded auto-remediation pass is an explicit, larger option
  (ADR-008 Q4), out of scope unless you choose it.
- Applied to BOTH worker paths (ADR-008 Q6, maintainer override): the
  spec-executor runs the gates post-loop on the cumulative diff, and the one-shot
  minion runs the same gates post-fix before its PR (there Gate B is a
  skip-with-notice until it has acceptance criteria). Both share one gate module
  and both are behind the default-off flags, so each path is unchanged when the
  flags are off. This is a bounded, post-step review — NOT a change to either
  path's existing loop.

---

## 8. Decisions I need from you

Both gates run **post-loop, once per spec** (§3, maintainer-directed), so there
is no per-task-vs-per-spec granularity question and no in-loop review retry.
Recorded in [adr/ADR-008-verification-gates.md](adr/ADR-008-verification-gates.md):

- **Q1 — Review engine:** in-container `kiro-cli` (Option 1, recommended),
  sub-agent reviewer (Option 2), or a different/stronger review model (Option 3)?
- **Q2 — One gate pass or two:** two distinct passes, Gate A then Gate B
  (recommended, clearer findings + independent flags), or one combined
  review+conformance pass (cheaper, muddier)?
- **Q3 — Spec inputs & Gate B:** OK to extend the payload/ingress with
  `requirements_md`/`design_md` (path or inline, `.kiro/specs/<feature>/`
  sibling default)? And if the spec is absent, skip-with-notice (recommended) vs
  block?
- **Q4 — Gate failure routing:** draft-PR-to-human (recommended; fits
  no-planner-in-container) vs an optional bounded post-loop auto-remediation pass
  (larger; adds `SPEC_MAX_REVIEW_RETRIES`)?
- **Q5 — Blocking vs advisory findings:** blocking (a failing gate ⇒ draft PR),
  advisory (findings recorded, PR stays non-draft), or **severity-gated**
  (recommended: high ⇒ draft, low ⇒ advisory)?
- **Q6 — Scope:** spec-executor only (recommended v1), or also add a post-fix
  review phase to the one-shot minion?
- **Q7 — Rollout flags/budgets:** ship behind `SPEC_REVIEW_ENABLED` /
  `SPEC_VERIFY_SPEC_ENABLED` defaulting **off** (recommended) or on; and a
  `SPEC_REVIEW_TIMEOUT_S` per-gate bound.

Nothing is built yet. Once you record the decisions in ADR-008, a fresh session
implements the post-loop review phase on top of the spec-executor.
