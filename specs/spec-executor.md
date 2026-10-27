# Spec: the spec-executor worker (whole `tasks.md` offload)

**Status:** ACCEPTED v1 (implemented). Realises [adr/ADR-002-task-shape.md](adr/ADR-002-task-shape.md)
Option C, plus ADR-003/004/005/006/007.
**Code:** `runtime/spec_agent.py` (outer loop), `runtime/tasks_md.py` (parser),
reusing `runtime/agent.py` building blocks.
**Related:** [ide-ingress.md](ide-ingress.md), [runtime.md](runtime.md),
[adr/README.md](adr/README.md)

---

## 1. What this worker is for

The original one-shot minion (`runtime/agent.py`) fixes ONE scoped thing (one
`kiro-cli` brief, auto-detected tests, up to 3 retries, one PR). That is the
`task_type="one_shot"` path and it is left untouched.

The **spec-executor** is a SECOND worker variant (`task_type="spec"`) for a
different shape of work. A developer follows a local spec-driven workflow (the
Kiro flow: `requirements.md`, then `design.md`, then `tasks.md`) and ends up
with a whole `tasks.md`: a markdown checklist of tens of tasks that together
implement one feature or change. Instead of grinding through that task list on
the laptop, the developer offloads the WHOLE `tasks.md` to a remote AgentCore
worker, which reads the entire list and iterates task-by-task.

The two variants share the same ingress, payload, queue, consumer, runtime
container, status table, and security controls. Only the pipeline body differs,
and the payload's `task_type` selects it.

---

## 2. Where the `tasks.md` comes from

A spec job carries the task list in one of two ways (see
[ide-ingress.md](ide-ingress.md) §5 for the payload):

1. **`tasks_md_path`** (preferred): a repo-relative path to the checklist, for
   example `.kiro/specs/<feature>/tasks.md`. The worker reads it AFTER cloning,
   so the content always matches the committed state of the branch it will push
   to. The path is traversal-validated at ingress
   (`orchestrator/parser.validate_repo_relative_path`) and RE-validated in the
   runtime (`runtime/spec_agent._validate_tasks_md_path`), with a final
   resolved-path containment check that the file really sits under the clone
   root. The path can never escape the clone or reach a shell as an option.
2. **`tasks_md`** (inline): the raw checklist text in the payload. Useful when
   the list is not committed yet, or for testing. Inline content wins over a
   path when both are present.

**Why a repo-relative path by default:** the committed `tasks.md` is the source
of truth, it stays versioned with the code, it keeps the payload small, and it
sidesteps shipping large free text through the ingress. Inline remains available
for the not-yet-committed case.

The checklist is parsed by a hand-rolled, stdlib-only parser
(`runtime/tasks_md.py`, `parse_tasks_md(text) -> list[SpecTask]`). A `SpecTask`
has `id`, `title`, `done`, and `body` (sub-bullets and `_Requirements:` refs
folded into the owning task). The parser tolerates both checkbox-first
(`- [ ] 1. Title`) and number-first (`1. [ ] Title`) orderings, a leading
`# Implementation Plan` heading, and interleaved prose. Input with no parseable
task raises `ValueError`.

---

## 3. The deterministic outer loop

The outer loop is **plain Python** (`run_spec_pipeline`): there is NO model
reasoning in the loop itself. This keeps the B1 principle the project chose (see
[runtime.md](runtime.md)): the model reasons ONLY inside each per-task
`kiro-cli` pass, never about the workflow.

```
receive TaskPayload (task_type="spec")
  → validate repo_clone_url / base_branch / fix_branch + allowlist (pre-clone)
  → setup git auth (gh credential helper; token never in URL/argv)
  → phase: cloning  → clone → create ONE fix branch
  → resolve + parse tasks.md (inline or repo-relative path, re-validated)
  → install deps once; snapshot the post-install porcelain as the staging baseline
  → phase: fixing
  → FOR each task IN order:
        → if already [x]: skip (not sent to the model)
        → budget checks (wall-clock, executed-task cap) → breach = blocker, STOP
        → phase: "task N/total"
        → FRESH per-task brief (this task only + minimal spec context)
        → per-task retry loop (≤ SPEC_MAX_TASK_ATTEMPTS):
              kiro-cli (brief on STDIN) → run tests
              pass → break ; fail → reset worktree + retry ; repeated fingerprint → early stop
        → tests still failing after the budget → blocker, STOP (no skip-ahead)
        → commit ONLY this task's diff (message names the task) ; advance the baseline
        → no committable diff → blocker, STOP
  → push the fix branch (0..n task commits)
  → open ONE PR (non-draft if every task completed; draft if it stopped at a blocker)
  → terminal status written to DynamoDB keyed on sonar_hash (the logical task_id)
```

### Fresh context per task

Each task gets a clean brief built by `_build_spec_task_brief`: the job
`summary`, a short leading slice of the `tasks.md` header for orientation, and
THIS task's `title` + `body`. It does NOT include the accumulated transcript of
previously completed tasks. This mirrors how local Kiro executes a `tasks.md`
and keeps per-task cost and variance bounded. A sub-agent per task is a natural
future refinement of the same idea. The only state carried across attempts of
the SAME task is the previous failing test output (and the previous non-fixing
diff), so a retry can learn from the last attempt.

The brief is always routed through `agent.redact()` and delivered on STDIN
(never argv), exactly like the one-shot `step_kiro_fix`.

---

## 4. Commits and the PR

- **One commit per completed task** on a single fix branch. The commit message
  names the task (built from `{task.id} {task.title}`, routed through
  `redact()`, with the logical `task_id` carried through `step_commit`). Each
  commit captures ONLY that task's diff: the loop advances the scoped-staging
  baseline (`agent._git_status_porcelain`) after every commit, so install churn
  and earlier tasks are never re-attributed.
- **The on-disk `tasks.md` checkbox is NOT rewritten.** Progress is recorded by
  the per-task commits and the PR body. Rewriting the checkbox would otherwise
  be swept into every task's scoped-staging commit. If checkbox rewrite is ever
  wanted it can be added as an explicit, separately staged step.
- **One PR per spec** (ADR-006), opened by `_open_spec_pr` with a spec-shaped
  body (`_build_spec_pr_body`, NOT the SonarQube-shaped `agent._build_pr_body`):
  - **Happy path** (every not-done task completed): a NON-draft PR whose body
    lists the completed tasks as an ordered sequence.
  - **Blocker** (see §5): a DRAFT PR whose body lists the completed tasks and
    names the blocked task plus a redacted reason.
  - The whole PR title and body are routed through `redact()`.

---

## 5. Stop-at-first-blocker

The loop STOPS the moment a task cannot be completed. It never skips ahead to
later tasks (a `tasks.md` is usually ordered and interdependent). A blocker is
any of:

- tests still failing after the per-task retry budget (`SPEC_MAX_TASK_ATTEMPTS`),
- a per-task `kiro-cli` timeout (`KIRO_TIMEOUT_S`) exhausting the budget,
- a task producing no committable diff (kiro did nothing), or
- a job budget breach (wall-clock or executed-task cap, see §6).

On a blocker the worker keeps the prior task commits, pushes the branch, and
opens ONE draft PR with a status report (completed vs blocked + the redacted
reason / truncated failing output). It returns the **retryable**
`tests_failed` status (same semantics as the one-shot `tests_failed`) so a later
re-dispatch is possible.

A blocker with ZERO completed tasks does NOT open an empty PR: it returns the
`tests_failed` result with no `pr_url`, mirroring the one-shot "no changes"
handling.

---

## 6. Budgets and guardrails (ADR-007)

Longer runs and higher per-job cost than the one-shot path mean the ADR-007
guardrails apply with more weight. The worker reads these at module level (like
`agent.MAX_FIX_ATTEMPTS` / `agent.KIRO_TIMEOUT_S`), so they are env-configurable
and a test reload picks up overrides:

| Env var | Default | Meaning |
|---------|---------|---------|
| `SPEC_MAX_TASK_ATTEMPTS` | `3` | Per-task retry budget (kiro + test attempts for ONE task before it is a blocker). |
| `SPEC_MAX_TASKS` | `50` | Cap on tasks EXECUTED in one job (already-done `[x]` tasks are skipped and do not count). `0`/unset = unlimited. |
| `SPEC_JOB_TIMEOUT_S` | `3600` | Overall wall-clock budget across the whole loop (`time.monotonic`). |
| `KIRO_TIMEOUT_S` | (reused) | Per-task `kiro-cli` timeout; reused from the one-shot path. |

A wall-clock or executed-task breach mid-loop is handled exactly like a blocker:
stop, keep prior commits, push, open a draft PR, and record which budget was
hit. These feed the broader ADR-007 story (per-actor dispatch quota, consumer
concurrency cap as the worker ceiling, alert-only budget) enforced at the
ingress and infrastructure layers.

---

## 7. Per-task progress (ADR-003)

The status API is polling-based (ADR-003). Alongside the coarse one-shot phases
(`cloning`, `fixing`, `opening_pr`), the spec-executor writes a per-task phase
string of the exact form `task N/total` (1-based, for example `task 7/20`) via
`agent.write_phase` before each task. `GET /tasks/{task_id}` surfaces it so the
IDE can show which task of how many is in flight. The terminal status is still
written keyed on `sonar_hash` (the logical `task_id`, ADR-004/005): `done` +
`pr_url` on the happy path, the retryable `tests_failed` + redacted reason on a
blocker, `failed` + redacted reason on an unexpected exception.

---

## 8. Security reuse

The spec-executor reuses the one-shot path's security controls unchanged:

- **`redact()`** masks `GITHUB_TOKEN` / `KIRO_API_KEY` / `WEBHOOK_SECRET` in
  every brief, commit message, PR title/body, log line, status write, and
  returned reason.
- **argv lists + `shell=False`** for every subprocess built from task-derived
  data (clone URL, branch names, commit message, PR fields, the kiro invocation).
- **Repo allowlist re-check** (`_validate_repo_allowed`) before cloning, plus
  clone-URL and branch-name validation, as defence in depth.
- **No token in argv/URL**: git/gh auth uses the gh credential helper; the token
  is read from the environment, never placed in a command or clone URL.
- **Brief on STDIN**, never argv.
- **Path safety**: `tasks_md_path` is re-validated in the runtime and contained
  under the clone root before it is read.

Every subprocess goes through the single `agent.RUNNER` / `agent._run`
indirection, so no second subprocess runner exists and tests can inject a
`FakeRunner`.

---

## 9. Which ADRs this realises

| ADR | How the spec-executor realises it |
|-----|-----------------------------------|
| [ADR-002](adr/ADR-002-task-shape.md) | Option C: a whole `tasks.md` run by a second worker variant selected by `task_type="spec"`, deterministic outer loop, fresh per-task context, one commit per task, one PR per spec, stop-at-first-blocker. |
| [ADR-003](adr/ADR-003-results-channel.md) | Polling status; adds the per-task `task N/total` phase. |
| [ADR-004](adr/ADR-004-idempotency-and-schema.md) | The logical `task_id` is carried in the physical `sonar_hash` key, so claim/lease/status write-back are reused. |
| [ADR-005](adr/ADR-005-payload-naming.md) | The generalised `TaskPayload` carries `task_type` plus the spec-only `tasks_md_path`/`tasks_md`; all runtime-read field names are unchanged. |
| [ADR-006](adr/ADR-006-result-delivery.md) | Result delivery is a GitHub PR (one per spec; draft on a blocker). |
| [ADR-007](adr/ADR-007-concurrency-and-cost-guardrails.md) | Per-task and per-job budgets (§6) on top of the ingress quota, concurrency cap, and alert-only budget. |

---

## 10. Deferred: intra-spec parallelism

v1 runs tasks **sequentially by default**. Intra-spec parallelism (running tasks
judged independent at the same time) is a **soft goal, not implemented**. It is
unsafe for v1 on a single fix branch: concurrent commits would corrupt the
ordered commit sequence, and absent reliable dependency metadata the worker
cannot know which tasks are truly independent. Pursuing it later needs a
dependency convention in `tasks.md` that the parser can read (and likely a
branch-or-worktree-per-lane strategy to keep commits clean). This is tracked as
a follow-up; until then, sequential execution is the contract.
