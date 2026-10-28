# ADR-008: Verification gates for the spec-executor (post-loop adversarial review + verify-vs-spec)

**Status:** ACCEPTED — recommendations taken for Q1–Q5 and Q7; **Q6 overridden
by the maintainer to apply the gates to BOTH the spec-executor and the one-shot
minion.** See "Accepted decisions" below.
**Context doc:** [../verification-gates.md](../verification-gates.md)
**Depends on:** [ADR-002](ADR-002-task-shape.md) (spec-executor exists),
[ADR-003](ADR-003-results-channel.md), [ADR-005](ADR-005-payload-naming.md),
[ADR-007](ADR-007-concurrency-and-cost-guardrails.md)

## Accepted decisions (authoritative)

| Q | Decision | Note |
|---|----------|------|
| **Q1** Review engine | **In-container `kiro-cli` review invocation(s)** | Reuse auth/redaction/RUNNER; worker owns the verdict format. |
| **Q2** One pass or two | **Two distinct passes** — Gate A (adversarial review) then Gate B (verify-vs-spec) | Independent verdicts + independent enable flags. |
| **Q3** Spec inputs / Gate B | **Extend payload/ingress with `requirements_md`/`design_md`** (repo-relative path, re-validated + clone-root-contained like `tasks_md_path`; or inline), **`.kiro/specs/<feature>/` sibling default**. **Absent spec → skip Gate B with notice** in the PR body (never block on a missing spec). | Gate A needs no new input. |
| **Q4** Gate-failure routing | **Draft-PR-to-human.** A failing gate opens a DRAFT PR flagged with which gate(s) failed + findings; commits are kept/pushed. **No auto-remediation loop**, so **`SPEC_MAX_REVIEW_RETRIES` does NOT exist.** | Human / fresh planning session decides next. |
| **Q5** Blocking vs advisory | **Severity-gated:** **high-severity** findings downgrade the PR to **draft**; **low-severity** are **advisory** (recorded in the PR body, PR stays non-draft). Applies to both gates. | The gate verdict schema must carry a severity per finding. |
| **Q6** Scope | **OVERRIDDEN → BOTH.** Apply the gates to the **spec-executor** (post-loop phase on the cumulative diff) **AND** add a **post-fix review phase to the one-shot minion** (`task_type="one_shot"` / the Jira-SonarQube path) that runs the same gates on the one-shot change before its PR. | Maintainer chose both, not spec-executor-only. See "One-shot integration" below. |
| **Q7** Rollout flags / budgets | Ship behind **`SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED`**, **default OFF** (opt-in per deployment). Add **`SPEC_REVIEW_TIMEOUT_S`** per gate-pass bound; gate passes count toward `SPEC_JOB_TIMEOUT_S`. | Flag names are shared by both paths (see one-shot note for the one-shot wall-clock). |

### One-shot integration (Q6 override)

The one-shot minion (`runtime/agent.py` `run_pipeline`) produces a single change
and opens one PR. With Q6 = BOTH, after its existing fix+test succeeds and before
`step_open_pr`, it runs the SAME two gates on the one-shot change's cumulative
diff:

- **Gate A (adversarial review)** — input = the one-shot diff + the ticket
  `description`/`summary` (the one-shot path's "intent"). No new input needed.
- **Gate B (verify-vs-spec)** — the one-shot path has **no Kiro spec**
  (`requirements.md`/`design.md`); by Q3's absent-spec rule, **Gate B is skipped
  with a notice** on the one-shot path. (It becomes meaningful only if a future
  one-shot source supplies acceptance criteria.) So in practice the one-shot path
  gains **Gate A** now and Gate B is a documented no-op there until it has a spec.
- **Routing/severity** identical (Q4/Q5): a high-severity Gate A finding
  downgrades the one-shot PR to draft with findings; low-severity is advisory.
- **Flags:** gated by the same `SPEC_REVIEW_ENABLED` (Gate A) /
  `SPEC_VERIFY_SPEC_ENABLED` (Gate B) flags, default off, so the one-shot path is
  byte-for-byte unchanged when the flags are off (preserving the existing
  one-shot tests). The per-gate `SPEC_REVIEW_TIMEOUT_S` applies; the one-shot
  path has no `SPEC_JOB_TIMEOUT_S`, so the gate timeout stands alone there.
- **Shared implementation:** the gate logic (brief construction, kiro-cli review
  invocation, verdict parse, severity routing, redaction) should live in ONE
  reusable module invoked by both `run_pipeline` (one-shot) and
  `run_spec_pipeline` (spec-executor), not duplicated.

**B1 note:** on the one-shot path the gates are still a bounded, post-fix,
code-orchestrated set of model passes before the PR — no outer loop is
introduced, so B1 holds there too.

## Context

The spec-executor's only per-task acceptance gate is "the repo's tests pass"
(see [../spec-executor.md](../spec-executor.md) §5). That implements BUILD +
a thin deterministic VERIFY from the AI-SDLC lifecycle pattern, but skips the two
gates that make a change *trustworthy* before a human reviews it:

- **adversarial REVIEW** (fresh context) of the implementation, and
- **VERIFY-VS-SPEC** — does the change satisfy the spec's acceptance criteria,
  not just the tests?

We want to add both **in the worker, before the PR**, without breaking the
project's **B1** principle (deterministic outer loop; model only inside bounded
steps).

## Design decision on placement (maintainer-directed)

**Both gates run OUTSIDE the coding loop, as a single post-loop review PHASE,
once per task group (spec), after every task has been built, tested, and
committed.** They are NOT interleaved per task.

Rationale: this keeps the coding loop purely BUILD + deterministic test-VERIFY
(no model-based gate inside it), and isolates all gate (model) reasoning in one
clearly bounded phase that runs on the *cumulative* branch diff. It is the
cleaner B1 story (the loop stays deterministic end to end; the gates are a
separate, bounded, deterministically-orchestrated phase) and it reviews the whole
change as one coherent unit instead of task-by-task fragments.

```
PHASE 1 — BUILD (coding loop, unchanged):
  FOR each task: fresh kiro-cli pass → run tests → commit one task  [existing]
  (stop-at-first-blocker still applies here, as today)

PHASE 2 — VERIFY (post-loop, NEW, runs once after all tasks complete):
  Gate A  adversarial review of the CUMULATIVE branch diff (fresh context)
  Gate B  verify-vs-spec: cumulative diff vs requirements/design acceptance
  → combine verdicts → open PR:
        both pass            → NON-draft PR (human = Gate 2)
        either fails         → DRAFT PR flagged with which gate failed + findings
```

Consequences of this placement for the questions below: there is **no per-task
vs per-spec granularity choice** (both gates are per-spec by construction), and
**no BUILD↔review loopback inside the loop** — a gate failure's routing is now a
post-loop decision (Q5), not a per-task retry.

## Decision (framework — options per question below)

**Committed regardless of the per-question choices:** the review phase is
**bounded** (a fixed number of gate passes, never an open-ended "review until
happy" loop) and **orchestrated by deterministic code** (the loop runs the gates
after it finishes, feeds them the cumulative diff + intent, and routes the
combined verdict to a draft/non-draft PR; the model only produces the
review/verdict inside each bounded pass). This preserves B1.

### Q1 — Review engine
- **Option 1 (recommended):** in-container `kiro-cli` review invocation(s) —
  reuses auth/redaction/RUNNER, no new infra; we own the verdict format.
- **Option 2:** sub-agent / separate reviewer process (stronger "independent"
  story; more moving parts in the image).
- **Option 3:** a different/stronger model for review than for build (best
  independence; extra cost + a second model dependency).

### Q2 — One gate pass or two?
Both gates run post-loop on the cumulative diff. Do they run as:
- **Two distinct passes (recommended):** one adversarial-review pass (Gate A) and
  one verify-vs-spec pass (Gate B), each with its own brief and verdict —
  clearer findings, independent enable flags, slightly more cost.
- **One combined pass:** a single review brief asked to produce both a code
  review and a spec-conformance verdict — cheaper, but muddier verdicts and
  harder to flag "which gate failed" cleanly.

### Q3 — Spec inputs & Gate B availability
- Extend the payload/ingress with `requirements_md`/`design_md` (repo-relative
  path, re-validated + clone-root-contained, like `tasks_md_path`; or inline),
  with a default `.kiro/specs/<feature>/` sibling lookup.
- If no spec acceptance source is present: **skip-with-notice (recommended)** —
  Gate B is skipped, the PR says spec-verification did not run — vs **block**
  (refuse a non-draft PR without a spec to verify against). (Gate A needs no new
  input; it works from the cumulative diff + the `tasks.md`/summary already
  present.)

### Q4 — Gate failure routing (post-loop)
- **Draft-PR-to-human (recommended):** if Gate A returns `CHANGES_REQUESTED`
  and/or Gate B returns `DOES_NOT_SATISFY`, open a **draft** PR flagged with which
  gate(s) failed + the findings; a human (or a fresh planning session) decides.
  Fits the no-autonomous-planner-in-container reality. The task commits are kept
  and pushed regardless, so no work is lost.
- **Bounded auto-remediation (optional, larger):** on a gate failure, run up to
  `SPEC_MAX_REVIEW_RETRIES` additional post-loop BUILD passes targeting the
  findings (a second bounded coding pass over the cumulative diff), then re-run
  the gates; still deterministic and bounded, but more model cost and a more
  complex phase. Only if you want it.

### Q5 — Blocking vs advisory findings
- **Blocking:** a failing gate downgrades the PR to draft (the default outcome of
  Q4's recommended option).
- **Advisory:** gate findings are recorded in the PR body but never downgrade it.
- **Severity-gated (recommended):** high-severity findings downgrade to draft;
  low-severity are advisory (recorded, PR stays non-draft). Applies to both gates.

### Q6 — Scope
- Spec-executor only (was the recommendation). The one-shot minion stays
  unchanged.
- **Also add a post-fix review phase to the one-shot path — ✅ CHOSEN (maintainer
  override).** Both paths get the gates (sharing one gate module); see "One-shot
  integration" above. On the one-shot path Gate B is a documented skip-with-notice
  until it has acceptance criteria, so it effectively gains Gate A now.

### Q7 — Budgets & rollout flags
- `SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED` flags to enable each gate
  independently — **default off (recommended)** for safe rollout, flip on per
  deployment — vs default on.
- A `SPEC_REVIEW_TIMEOUT_S` (per gate pass) bound, counting toward
  `SPEC_JOB_TIMEOUT_S`.
- `SPEC_MAX_REVIEW_RETRIES` is only needed if Q4 chooses bounded
  auto-remediation; otherwise it does not exist.

## Consequences

- **More model calls per job** (the post-loop phase) → cost and latency rise;
  ADR-007 budgets (`SPEC_JOB_TIMEOUT_S`, per-actor quota, concurrency cap) matter
  more; new per-gate timeout + feature flags are added. Default-off flags make
  the gates opt-in until proven.
- **Coding loop is unchanged** — Phase 1 keeps today's behaviour and tests; the
  work is a new, self-contained Phase 2 plus the Q3 payload/ingress change and a
  PR-body "Verification" section.
- **Gate B needs the spec in the worker** → the Q3 payload/ingress change and the
  `.kiro/specs/<feature>/` sibling-lookup convention.
- **Whole-change review** — gates see the cumulative diff, so they can catch
  cross-task issues (inconsistencies between tasks) a per-task review would miss;
  the trade-off is a finding cannot always be attributed to a single task.
- **Stronger pre-human trust** — the human Gate 2 reviews a change that passed
  tests + adversarial review + (where available) spec conformance; a draft PR
  states which gate failed.
- **B1 preserved** — the loop is deterministic end to end; the gate phase is a
  bounded, code-orchestrated set of model passes. The ADR forbids an open-ended
  review loop.
- **Verdict contract** — whichever engine (Q1), the gates need a stable,
  machine-parseable verdict schema (status + severity-tagged findings) the
  deterministic phase can route on; defined in the implementing spec/PR.
- **Interaction with stop-at-first-blocker** — if Phase 1 stops at a blocker
  (not all tasks done), the gates do NOT run (there is no complete change to
  verify); the draft PR is opened for the blocker as today. The gate phase runs
  only on a fully-built task group.

## Status / next step

ACCEPTED (see "Accepted decisions"). Implementation proceeds: a shared gate
module (in-container `kiro-cli` review + verify-vs-spec, severity-gated,
redaction, draft/non-draft routing) invoked by the spec-executor's post-loop
phase (`runtime/spec_agent.py`) AND by the one-shot minion before its PR
(`runtime/agent.py`), both behind the default-off `SPEC_REVIEW_ENABLED` /
`SPEC_VERIFY_SPEC_ENABLED` flags, per [../verification-gates.md](../verification-gates.md).
