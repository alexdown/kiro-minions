# Spec: Kiro IDE task ingress (remote worker hand-off)

**Status:** ACCEPTED v1 — implemented. ADR-001..007 accepted with the
recommended options; the ingress (`POST /tasks`), status poll
(`GET /tasks/{task_id}`), per-actor quota, and audit log are live.
**Author:** kiro-minions agent (first pass)
**Related:** [overview.md](overview.md), [payload.md](payload.md), [runtime.md](runtime.md), [adr/README.md](adr/README.md)

---

## 1. Problem

A customer runs a Kiro IDE (VSCode) extension that **enforces a dev workflow and
runs all agents locally**. Local execution makes the developer's laptop the
compute bottleneck: parallelism is capped at one machine, long tasks block the
editor, and heavy work drains battery / competes with the dev's own tools.

They want to **hand tasks off to a worker agent on AgentCore** so the laptop is
no longer the bottleneck and many tasks can run in parallel in the cloud.

kiro-minions already runs exactly this kind of remote worker — a deterministic
pipeline in an AgentCore Runtime — but today its **only ingress is a Jira
webhook** for SonarQube findings. This spec proposes a **second ingress**: an
authenticated API that the IDE extension calls to dispatch a task, plus a
**results channel** so the IDE can show progress and the outcome.

### Goals

- Let the IDE dispatch a task to a remote AgentCore worker and get a result
  (a PR, a branch, or a status) back.
- **Reuse the existing downstream unchanged** where possible: SQS work queue,
  DLQ, idempotency/lease, status write-back, and the security controls (repo
  allowlist, secret redaction, no-token-in-argv, `shell=False`).
- Scale parallelism with AWS concurrency, not laptop cores.
- Keep the deterministic-pipeline principle (B1): no new outer model loop in the
  orchestrator layer.

### Non-goals (for v1 of this feature)

- Replacing the Jira ingress (both coexist).
- Building the VSCode extension itself (the customer owns that; we define the
  contract it calls).
- Streaming the full agent transcript to the IDE (status milestones only in v1).
- An open-ended, model-in-a-loop worker. ADR-002 landed on Option C: a whole
  `tasks.md` is run by a deterministic spec-executor whose outer loop is plain
  code, with model reasoning confined to each per-task pass (see §4 and ADR-002).
  There is no open-ended outer model loop.

---

## 2. What we can reuse as-is

The pipeline today is Jira-shaped only at its two ends:

| Layer | Jira-specific? | Reuse for IDE ingress |
|-------|----------------|------------------------|
| Webhook parser (`parser.py`: label gate, Part-1 header, `sonar-hash`) | **Yes** | Replaced by an IDE request parser |
| Webhook auth (`verify_webhook`: HMAC / shared secret) | **Yes** | Replaced by per-developer auth (ADR-001) |
| `TicketPayload` contract | Partly (field names) | Generalise to a `TaskPayload` (§5) |
| Idempotency / lease (`_claim_job`, DynamoDB) | No | Reuse; key becomes a generic `task_id` (ADR-004) |
| SQS `JobsQueue` + DLQ + consumer Lambda | No | **Reuse unchanged** |
| AgentCore Runtime pipeline (`agent.py`) | Partly (assumes a scoped fix) | Reused unchanged for `one_shot`; a second variant (`spec_agent.py`) reuses its building blocks for `spec` (ADR-002 Option C) |
| Status write-back to DynamoDB | No | Reuse; add a read API in front (§6) |
| Security controls (allowlist, redaction, argv, shell=False) | No | **Reuse unchanged** |

The expensive, security-sensitive, well-tested core is reusable. The new surface
is: **(a) an authenticated ingress, (b) a payload mapping, (c) a results API.**

---

## 3. Proposed architecture

```
  Kiro IDE (VSCode ext)                         AWS
  ┌───────────────────┐
  │ dev assigns a task│
  │ ext packages it   │   HTTPS + per-dev auth (ADR-001)
  │ POST /tasks       │ ───────────────────────────────►  API Gateway (HTTP API)
  └───────────────────┘                                        │
           ▲                                                    ▼
           │                                            ┌────────────────┐
           │                                            │ Ingress Lambda │  (NEW)
           │                                            │ authn/authz    │
           │                                            │ validate+map   │  IDE req → TaskPayload
           │                                            │ claim task_id  │  (DynamoDB lease — reused)
           │                                            │ enqueue → 202  │
           │                                            └───────┬────────┘
           │                                                    │ sqs:SendMessage (TaskPayload JSON)
           │                                                    ▼
           │                                            ┌────────────────┐
           │                                            │   JobsQueue    │  SQS (+ DLQ)  — REUSED
           │                                            └───────┬────────┘
           │                                                    ▼
           │                                            ┌────────────────┐
           │                                            │   Consumer     │  — REUSED
           │                                            │ invoke runtime │
           │                                            └───────┬────────┘
           │                                                    ▼
           │                                   ┌───────────────────────────────┐
           │                                   │ AgentCore Runtime (N parallel) │  — REUSED pipeline
           │                                   │  clone → kiro → test → PR      │
           │                                   └───────────────┬───────────────┘
           │                                                   │ write status
           │  GET /tasks/{id}  (poll)  or  WebSocket (stream)  ▼
           └───────────────────────────────────────────  DynamoDB (job/task status)
                              ▲                                 │
                       Status API Lambda (NEW)  ◄───────────────┘
```

**One added ingress Lambda, one added status API, everything between SQS and
DynamoDB reused.** The IDE never talks to AgentCore directly — it only sees the
two HTTP endpoints, which keeps AWS creds and the runtime ARN server-side.

---

## 4. The crux: what *is* an IDE task? (drives most of the design)

The Jira ingress handles a **narrow, structured task**: "fix this specific
SonarQube finding in this file." The one-shot deterministic pipeline is built
around that shape: one `kiro-cli` brief, auto-detected tests, up to 3 retries,
one PR.

An IDE task is bigger. ADR-002 is **ACCEPTED as Option C**: the headline IDE
shape is a **whole `tasks.md`** produced by a local spec-driven workflow (the
Kiro flow: `requirements.md`, then `design.md`, then `tasks.md`, often tens of
tasks). The developer offloads the ENTIRE task list to a remote worker that
reads it and iterates task-by-task. Both shapes are supported, selected by a
`task_type` discriminator on the payload (§5):

- **`task_type="one_shot"` (the original scoped path, retained).** A bounded
  task: repo + base branch + an optional target (file/area) + a natural-language
  `instruction` + acceptance = tests pass. It maps 1:1 onto the existing
  pipeline (`description` → kiro-cli brief) and `runtime/agent.py` is reused
  unchanged.
- **`task_type="spec"` (the headline IDE shape).** A whole `tasks.md` (inline or
  referenced in the repo) run by a NEW, SEPARATE **spec-executor** worker
  variant (`runtime/spec_agent.py`). Its outer loop is deterministic plain code
  (model reasoning stays inside each per-task pass, faithful to B1); it runs a
  fresh per-task brief, commits once per completed task on a single branch, and
  opens one PR per spec with stop-at-first-blocker semantics.

ADR-002 is the authority for this decision. The spec-executor's full design
(tasks.md source, the outer loop, budgets, the `task N/total` progress phase,
security reuse, and the deferred parallelism follow-up) lives in
[spec-executor.md](spec-executor.md). The original one-shot minion
(`runtime/agent.py`, the Jira/SonarQube path) is left behaviourally unchanged.

---

## 5. Payload contract (generalising `TicketPayload` → `TaskPayload`)

The runtime consumes a `TaskPayload` (a superset of the original
`TicketPayload`, see [payload.md](payload.md) and ADR-005). The IDE ingress
produces the **same downstream contract** so SQS / consumer / runtime do not
change. A `task_type` discriminator selects which pipeline runs:

- `task_type="one_shot"` (default when absent): the original scoped-fix path.
  `description`, `summary`, and `repo_clone_url` are **required**.
- `task_type="spec"`: the whole-`tasks.md` spec-executor path. It requires
  `repo_clone_url`, `summary` (used for the PR title), and EITHER
  `tasks_md_path` (a traversal-validated repo-relative path, read after the
  clone) OR inline `tasks_md`. For a spec job `description` is **optional** (the
  `tasks.md` is the work).

**one_shot example:**

```jsonc
{
  "task_id":        "ide-7f3c…",          // idempotency key (ADR-004)
  "sonar_hash":     "ide-7f3c…",          // physical key carries the task_id (ADR-004/005)
  "source":         "kiro-ide",           // vs "jira"; for routing/audit
  "actor":          "dev@customer.com",   // who dispatched (from auth, ADR-001)
  "task_type":      "one_shot",           // scoped single fix (ADR-002)
  "repo_clone_url": "https://github.com/org/repo.git",
  "base_branch":    "main",
  "fix_branch":     "kiro/ide-7f3c…",     // or caller-supplied (validated)
  "file_to_fix":    "src/foo.ts:42",      // optional scope hint
  "description":    "…natural-language instruction = kiro-cli brief…",
  "summary":        "short title for the PR/commit",
  "acceptance":     "tests_pass"          // v1 fixed; future: lint, typecheck…
}
```

**spec example (whole `tasks.md`):**

```jsonc
{
  "task_id":        "ide-9a21…",
  "sonar_hash":     "ide-9a21…",          // physical key carries the task_id
  "source":         "kiro-ide",
  "actor":          "dev@customer.com",
  "task_type":      "spec",               // whole tasks.md → spec-executor (ADR-002 Option C)
  "repo_clone_url": "https://github.com/org/repo.git",
  "base_branch":    "main",
  "fix_branch":     "kiro-minions/spec-ide-9a21…",
  "tasks_md_path":  ".kiro/specs/checkout-redesign/tasks.md", // repo-relative; read after clone
  // "tasks_md":    "# Implementation Plan\n- [ ] 1. …",      // OR inline, instead of a path
  "summary":        "Execute the checkout-redesign spec",     // required (PR title)
  "acceptance":     "tests_pass"
}
```

- Fields that already exist keep their names and validation
  (`validate_clone_url`, `validate_branch_name`) unchanged.
- The generic idempotency key is `task_id` (ADR-004); it is carried in the
  **physical `sonar_hash` partition-key attribute** on the shared table so the
  claim/lease logic, the consumer, and the runtime are reused unchanged. The
  Jira-only `sonar-hash` *label* remains the Jira path's source for that value.
- `task_type` selects the pipeline (`one_shot` vs `spec`); it defaults to
  `one_shot` when absent so an existing IDE request keeps its behaviour.
- The spec-only fields are `tasks_md_path` (a conservative repo-relative path
  matching `^[A-Za-z0-9._/-]{1,256}$`, no leading `/`, no `..` segment, so it
  can neither escape the clone nor reach a shell as an option) and `tasks_md`
  (inline content). At least one is required for a spec job; inline wins when
  both are present. They are absent/None for a one_shot job.
- A caller MAY supply an optional `idempotency_key` to override the derived
  `task_id`. Because this value becomes the physical `sonar_hash` partition key
  on a table shared with the Jira path and the per-actor `quota#…` counters, it
  is **constrained**: it is forced into the `ide-` namespace and must match
  `^[A-Za-z0-9._-]{1,128}$` (after an optional leading `ide-`). This rejects
  `#` (so it cannot land in the `quota#…` namespace), whitespace, and overlong
  values, so a client can neither collide with a Jira hash / quota counter nor
  target another tenant's row via the status API's by-key read.
- The one_shot runtime path needs **no change**: `description` stays the
  kiro-cli brief and `acceptance=tests_pass`. The spec path adds the
  spec-executor worker (see [spec-executor.md](spec-executor.md)).

---

## 6. Results / feedback channel

Jira ingress is fire-and-forget (the PR shows up later). A developer in an IDE
expects feedback. Options (ADR-003):

- **Option A — Polling (recommended v1).** `GET /tasks/{task_id}` returns the
  current status row from DynamoDB (`queued` / `in_flight` / `done`+`pr_url` /
  `failed` / `tests_failed` + a redacted reason). The extension polls every few
  seconds. Simplest, stateless, cheap, reuses the status row we already write.
  The endpoint is read-only and scopes IDE-sourced rows to their owner: a
  verified identity may only read its OWN IDE tasks (the row records the
  dispatching `actor`); another identity gets `404` (existence is not leaked).
  Jira-sourced rows have no per-developer owner and stay readable by any
  verified identity.
- **Option B — Streaming (WebSocket / SSE via API Gateway).** Near-real-time
  milestones ("cloning", "running tests", "PR opened"). Better UX, more moving
  parts (connection state, fan-out, auth on the socket).

**Recommendation:** polling for v1 (the status row already exists), add a status
`detail`/`phase` field so the IDE can show coarse progress; revisit streaming
once the hand-off is proven. The runtime may need to emit a couple of
intermediate status updates (e.g. `phase=cloning|fixing|testing|opening_pr`) for
a decent polling UX — small, additive change.

---

## 7. How the result gets back to the dev

The laptop has local, possibly-uncommitted state; the worker clones from the
remote. The contract must be explicit (ADR-006):

- **Option A — PR (like today, recommended).** Worker pushes a branch and opens
  a PR; the IDE surfaces the PR link. Fits the customer's "enforced dev
  workflow" and needs zero new mechanism.
- **Option B — Branch only.** Worker pushes a branch; the dev pulls it locally.
  Lighter-weight, but skips review gates the customer may rely on.
- **Option C — Patch artifact.** Worker produces a diff the IDE applies locally.
  Most "local-feeling", but loses the clean-clone isolation and needs a new
  artifact channel.

A prerequisite either way: the worker operates on **pushed/committed** state, so
the IDE must ensure the base branch (and any WIP it wants acted on) is pushed
before dispatch. This is a workflow contract to confirm with the customer.

---

## 8. Security & multi-tenant concerns (new vs the webhook)

- **Per-developer identity (ADR-001).** A shared webhook secret is not enough:
  we need to know *who* dispatched, scope repo access to their permissions, and
  audit it. Options: OIDC via their IdP/SSO, Cognito, or short-lived tokens the
  extension already holds. This also feeds `actor` in the payload for audit.
- **Repo authorization.** Today's static `REPO_ALLOWLIST` (defence in depth)
  stays, but per-dev dispatch means we should also check the *actor* is allowed
  to target that repo — not just that the repo is globally allowlisted.
- **GitHub credentials.** The worker uses a server-side `GITHUB_TOKEN` via the gh
  credential helper. For per-dev attribution we may want the PR authored as a
  bot with the dev noted, or (bigger) per-dev tokens. Scope decision for later.
- **Concurrency & cost guardrails become required, not optional.** The whole
  point is more parallelism: Lambda reserved concurrency, AgentCore session
  limits, Bedrock token spend, and a per-actor dispatch rate/quota all move from
  "nice to have" (backlog I3/I5) to **required** for this feature. See ADR-007.
- **Secret redaction, no-token-in-argv, shell=False** carry over unchanged.

---

## 9. Phasing (proposed)

1. **Spike / design (this doc + ADRs).** ADR-001..007 are ACCEPTED (ADR-002 as
   Option C). Done.
2. **Thin PoC:** new ingress Lambda + `/tasks` route that authenticates, maps an
   IDE request → `TaskPayload`, claims, and enqueues onto the **existing** SQS.
   Reuse everything downstream. Add `GET /tasks/{id}` reading the status row.
   Done.
3. **IDE integration:** customer extension calls `/tasks` + polls `/tasks/{id}`.
4. **Hardening:** per-actor quotas, concurrency caps, cost alarms, audit log,
   intermediate status phases for progress UX. Done.
5. **Spec-executor worker (ADR-002 Option C, done):** the `task_type="spec"`
   variant that runs a whole `tasks.md` (deterministic outer loop, one commit
   per task, one PR per spec, stop-at-first-blocker). See
   [spec-executor.md](spec-executor.md).
6. **(Follow-up) Intra-spec parallelism** for independent tasks: deferred, needs
   a `tasks.md` dependency convention (see [spec-executor.md](spec-executor.md)
   §10).

Each phase ships with unit tests using the existing fakes; no real AWS/GitHub in
tests; keep the diff focused per phase.

---

## 10. Decisions (ADRs)

All of the decisions below are **ACCEPTED** and implemented; the full records
are in [adr/README.md](adr/README.md):

- **ADR-001**: developer authentication: OIDC via the customer's existing IdP.
- **ADR-002**: task shape: **Option C**, a new "spec-executor" worker variant
  that runs a whole `tasks.md` (plus the retained `one_shot` scoped path),
  selected by `task_type`. See [spec-executor.md](spec-executor.md).
- **ADR-003**: results channel: polling for v1 (with a `phase` field, including
  the spec-executor's `task N/total`).
- **ADR-004**: idempotency key & DynamoDB schema: a generic `task_id` carried
  in the physical `sonar_hash` key; additive schema.
- **ADR-005**: payload naming: generalise to `TaskPayload`, keep field names,
  add `task_type` and the spec-only fields.
- **ADR-006**: result delivery: a GitHub PR (one per spec; draft on a blocker).
- **ADR-007**: concurrency, quota, and cost guardrails for scaled parallelism,
  with per-task/per-job budgets in the spec-executor.
