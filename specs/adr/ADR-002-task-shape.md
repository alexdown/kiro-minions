# ADR-002: Task shape for v1 — scoped vs open-ended

**Status:** ACCEPTED — **Option C** (new "spec-executor" worker variant, separate
from the original one-shot minion). This supersedes an earlier interim record
that had accepted Option A; the maintainer subsequently chose a multi-task
spec-executor as the IDE task shape.
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §4

## Context

The existing AgentCore Runtime (`runtime/agent.py`) is a **deterministic B1
pipeline**: one `kiro-cli` brief, auto-detected tests, up to `MAX_FIX_ATTEMPTS`
retries, one PR. It is built for a **bounded, verifiable task** ("fix this
finding; success = tests pass"). This is what makes it cheap, predictable, and
testable, and it is the design the project deliberately chose over a managed
model loop (see [../runtime.md](../runtime.md)).

"Work assigned from an IDE" can mean very different things, and the answer
determines whether we reuse this runtime or build a richer one.

## Options

### Option A — Scoped tasks (recommended v1)
The IDE sends a bounded task: repo + base branch + a target (file/area) + a
natural-language instruction + acceptance = *tests pass*. Maps ~1:1 onto today's
pipeline (`description` → kiro-cli brief).

- **Pros:** reuses the runtime unchanged; keeps B1 (no outer model loop);
  verifiable acceptance (tests); lowest cost/latency; fastest to ship; proves the
  hand-off and the scaling story with minimal new surface.
- **Cons:** constrains what a dev can assign; "implement a feature" doesn't fit;
  acceptance limited to the auto-detected test suite.

### Option B — Open-ended tasks
"Implement feature X across the repo." Likely needs a plan→code→review loop (a
stateful, model-in-a-loop worker), not the single-shot deterministic pipeline.

- **Pros:** much more capable; covers the general "assign any work" ask.
- **Cons:** reopens the outer-model-loop question B1 avoided; new and less
  predictable failure modes; higher and more variable cost per task; harder to
  define "done" (no simple acceptance gate); significantly larger build and test
  surface; multi-tenant cost risk is larger.

### Option C — Both, via two runtime variants
Keep the deterministic runtime for scoped tasks; add a *separate* richer worker
for open-ended ones, selected by a `task_type` field.

- **Pros:** doesn't compromise the deterministic path; grows capability without
  regressing the proven core.
- **Cons:** two runtimes to build/operate; only worth it once Option A is proven.

## Decision

**ACCEPTED: Option C — a new, separate "spec-executor" worker variant.** The IDE
task is NOT a single scoped fix; it is a whole **`tasks.md`** produced by a local
spec-driven workflow (Kiro-style `requirements.md` → `design.md` → `tasks.md`,
often tens of tasks). The developer offloads the entire task list to a remote
worker that reads it and iterates.

The original one-shot deterministic pipeline (`runtime/agent.py`, the Jira/
SonarQube "minion") is **left untouched**. The spec-executor is a **second worker
variant**, selected by `task_type` on the payload (ADR-005).

**Spec-executor behaviour (v1):**

- **Input:** a `tasks.md` (Kiro spec-tasks format: a markdown checklist of tasks,
  each with sub-bullets / requirement refs) plus the repo context. The task list
  may be delivered inline in the payload or referenced in the repo.
- **Iteration loop (deterministic outer loop, model inside each task):** parse
  `tasks.md` → for each not-yet-complete task, run a **fresh-context** coding
  pass (a per-task `kiro-cli` invocation or a sub-agent) scoped to that one task
  → run tests → **one commit per completed task** → mark the task complete →
  move to the next. The outer loop is plain deterministic code (faithful to B1:
  the model reasons only *inside* each per-task pass, not about the workflow).
- **Fresh context per task:** each task gets a clean brief (the task text + spec
  context), NOT the accumulated transcript of previous tasks — this mirrors how
  local Kiro executes a `tasks.md` and keeps per-task cost/variance bounded.
- **One PR per spec:** all per-task commits land on one fix branch; a single PR
  is opened for the whole `tasks.md` (ADR-006). Commit-per-task keeps the PR
  reviewable as an ordered sequence.
- **Partial failure = stop-at-first-blocker:** if a task cannot be completed (or
  its tests won't pass after the retry budget), the worker **stops**, commits the
  tasks done so far, opens a **draft PR**, and writes a status report listing
  completed vs blocked tasks and why. It does NOT skip ahead.
- **Ordering & optional parallelism:** tasks are executed **sequentially by
  default** (a `tasks.md` is usually ordered and interdependent). The worker/
  orchestrator **MAY** run tasks it judges independent in parallel as an
  optimisation — this is a **soft goal, not a hard requirement** for v1, and must
  never reorder dependent tasks or corrupt the single-branch commit sequence.
  Absent reliable dependency metadata, default to sequential.

**Acceptance** is per-task (the repo's tests pass for that task's change) plus
"all tasks complete" for the spec; a blocked task downgrades the PR to draft.

**Questions this resolved (answered by the maintainer):**
1. Task shape = a whole `tasks.md` (multi-task), spec-driven offload — not a
   single scoped fix.
2. PR granularity = **one PR per spec, one commit per completed task.**
3. Partial failure = **stop at first blocker**, draft PR + report.
4. Parallelism = sequential by default; orchestrator may parallelise independent
   tasks (soft goal, not required).

## Consequences

- A **new worker** is built (separate container/entrypoint or a `task_type`
  branch in the runtime), leaving the one-shot minion unchanged. This is a larger
  build than Option A and needs: a `tasks.md` parser, a deterministic per-task
  iteration loop with fresh context, per-task commit/complete bookkeeping,
  stop-at-first-blocker handling, and a spec-level status report.
- The outer loop stays deterministic; model reasoning is confined to each
  per-task coding pass (optionally a sub-agent) — the B1 principle holds.
- Longer runtime budgets and higher per-job cost than the one-shot path →
  ADR-007 guardrails (per-actor quota, worker cap, budget alarms) apply with more
  weight; a per-task timeout and an overall job timeout are needed.
- Feeds ADR-005 (a `task_type` field selects spec-executor vs one-shot; the
  payload carries the `tasks.md` / spec reference) and ADR-003 (the `phase`/
  status should surface per-task progress, e.g. "task 7/20").
- Intra-spec parallelism is deferred-by-default; if pursued, `tasks.md` needs a
  dependency convention the parser can read. Noted as a follow-up.
