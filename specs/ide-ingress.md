# Spec: Kiro IDE task ingress (remote worker hand-off)

**Status:** IMPLEMENTED. The ingress (`POST /tasks`), status poll
(`GET /tasks/{task_id}`), per-actor quota, and audit log are live, built on
ADR-001..007.
**Related:** [overview.md](overview.md), [payload.md](payload.md), [runtime.md](runtime.md), [adr/README.md](adr/README.md)

---

## 1. What this ingress is for

A customer runs a Kiro IDE (VSCode) extension that **enforces a dev workflow and
runs all agents locally**. Local execution makes the developer's laptop the
compute bottleneck: parallelism is capped at one machine, long tasks block the
editor, and heavy work drains battery / competes with the dev's own tools.

The Kiro IDE ingress lets a developer **hand tasks off to a worker agent on
AgentCore** so the laptop is no longer the bottleneck and many tasks can run in
parallel in the cloud. It is one of the two ingresses into kiro-minions (the
other is the Jira/SonarQube webhook). The IDE extension calls an authenticated
API to dispatch a task, and a **results channel** lets the IDE show progress and
the outcome.

### What it provides

- The IDE dispatches a task to a remote AgentCore worker and gets a result (a PR
  and a status) back.
- Parallelism scales with AWS concurrency, not laptop cores.
- The deterministic-pipeline principle (B1) holds: there is no outer model loop
  in the orchestrator layer.

### Boundaries

- The VSCode extension itself is the customer's (this spec defines the contract
  it calls).
- The results channel reports status milestones, not the full agent transcript.
- The worker is not an open-ended model-in-a-loop. A `spec` task is run by the
  deterministic spec-executor whose outer loop is plain code, with model
  reasoning confined to each per-task pass (see §4 and ADR-002).

---

## 2. How the ingress maps onto the shared spine

The IDE ingress maps an IDE request to the shared `TaskPayload`; everything
downstream is the same shared spine: the SQS `JobsQueue`, the DLQ, the consumer
Lambda, the AgentCore Runtime, the status write-back, and the security controls
(repo allowlist, secret redaction, no-token-in-argv, `shell=False`). Only the
ingress surface is IDE-specific:

| Layer | IDE-specific? | Behaviour |
|-------|---------------|-----------|
| Request parser | **Yes** | An IDE request parser (the Jira path uses `parser.py`: label gate, Part-1 header, `sonar-hash`) |
| Authentication | **Yes** | Per-developer OIDC/JWT auth (ADR-001); the Jira path uses `verify_webhook` HMAC / shared secret |
| `TaskPayload` contract | Shared | One contract for both ingresses (§5) |
| Idempotency / lease (`_claim_job`, DynamoDB) | Shared | Key is the generic `task_id` (ADR-004) |
| SQS `JobsQueue` + DLQ + consumer Lambda | Shared | The same queue, DLQ, and consumer |
| AgentCore Runtime pipeline | Shared | `agent.py` for `one_shot`, `spec_agent.py` for `spec` (ADR-002 Option C) |
| Status write-back to DynamoDB | Shared | The same status row, with a read API in front (§6) |
| Security controls (allowlist, redaction, argv, shell=False) | Shared | Identical on both ingresses |

The expensive, security-sensitive, well-tested core is shared. The IDE-specific
surface is: **(a) an authenticated ingress, (b) a payload mapping, (c) a results
API.**

---

## 3. Architecture

```
  Kiro IDE (VSCode ext)                         AWS
  ┌───────────────────┐
  │ dev assigns a task│
  │ ext packages it   │   HTTPS + per-dev auth (ADR-001)
  │ POST /tasks       │ ───────────────────────────────►  API Gateway (HTTP API)
  └───────────────────┘                                        │
           ▲                                                    ▼
           │                                            ┌────────────────┐
           │                                            │ Ingress Lambda │
           │                                            │ authn/authz    │
           │                                            │ validate+map   │  IDE req → TaskPayload
           │                                            │ claim task_id  │  (DynamoDB lease)
           │                                            │ enqueue → 202  │
           │                                            └───────┬────────┘
           │                                                    │ sqs:SendMessage (TaskPayload JSON)
           │                                                    ▼
           │                                            ┌────────────────┐
           │                                            │   JobsQueue    │  SQS (+ DLQ)
           │                                            └───────┬────────┘
           │                                                    ▼
           │                                            ┌────────────────┐
           │                                            │   Consumer     │
           │                                            │ invoke runtime │
           │                                            └───────┬────────┘
           │                                                    ▼
           │                                   ┌───────────────────────────────┐
           │                                   │ AgentCore Runtime (N parallel) │
           │                                   │  clone → kiro → test → PR      │
           │                                   └───────────────┬───────────────┘
           │                                                   │ write status
           │  GET /tasks/{id}  (poll)                          ▼
           └───────────────────────────────────────────  DynamoDB (job/task status)
                              ▲                                 │
                         Status API Lambda  ◄──────────────────┘
```

The IDE talks only to the two HTTP endpoints (`POST /tasks` to dispatch, the
status API to poll); it never talks to AgentCore directly, which keeps AWS creds
and the runtime ARN server-side.

---

## 4. The two task shapes (drives most of the design)

A task comes in one of two shapes, selected by a `task_type` discriminator on
the payload (§5, ADR-002 Option C). Both shapes are supported through this
ingress:

- **`task_type="one_shot"`.** A bounded, scoped task: repo + base branch + an
  optional target (file/area) + a natural-language `instruction` + acceptance =
  tests pass. It maps 1:1 onto the `one_shot` pipeline (`description` → kiro-cli
  brief) in `runtime/agent.py`. This is also the shape the Jira ingress uses.
- **`task_type="spec"`.** A whole `tasks.md` (inline or referenced in the repo)
  produced by a local spec-driven workflow (the Kiro flow: `requirements.md`,
  then `design.md`, then `tasks.md`, often tens of tasks). The developer offloads
  the ENTIRE task list to the **spec-executor** worker
  (`runtime/spec_agent.py`), which reads it and iterates task-by-task. Its outer
  loop is deterministic plain code (model reasoning stays inside each per-task
  pass, faithful to B1); it runs a fresh per-task brief, commits once per
  completed task on a single branch, and opens one PR per spec with
  stop-at-first-blocker semantics.

ADR-002 records this decision. The spec-executor's full design (tasks.md source,
the outer loop, budgets, the `task N/total` progress phase, security reuse, and
the deferred parallelism follow-up) lives in
[spec-executor.md](spec-executor.md).

---

## 5. Payload contract (`TaskPayload`)

The runtime consumes a `TaskPayload`, the single shared contract (the historical
`TicketPayload` generalised; see [payload.md](payload.md) and ADR-005). The IDE
ingress produces the same contract both ingresses use, so the SQS queue, the
consumer, and the runtime are identical for either source. A `task_type`
discriminator selects which pipeline runs:

- `task_type="one_shot"` (default when absent): the scoped-fix path.
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
- The one_shot runtime path uses `description` as the kiro-cli brief with
  `acceptance=tests_pass`. The spec path runs the spec-executor worker (see
  [spec-executor.md](spec-executor.md)).

---

## 6. Results / feedback channel

The Jira ingress is fire-and-forget (the PR shows up later); a developer in an
IDE expects feedback. The results channel is **polling** (ADR-003):
`GET /tasks/{task_id}` returns the current status row from DynamoDB (`queued` /
`in_flight` / `done`+`pr_url` / `failed` / `tests_failed` + a redacted reason).
The extension polls every few seconds. It is stateless and cheap and reuses the
status row the runtime already writes.

The endpoint is read-only and scopes IDE-sourced rows to their owner: a verified
identity may only read its OWN IDE tasks (the row records the dispatching
`actor`); another identity gets `404` (existence is not leaked). Jira-sourced
rows have no per-developer owner and stay readable by any verified identity.

The status row carries a coarse `phase` field so the IDE can show progress; the
runtime emits intermediate updates (`phase=cloning|fixing|testing|opening_pr`).
Streaming over a WebSocket (near-real-time milestones at the cost of connection
state, fan-out, and socket auth) was considered in ADR-003 and left for a future
revisit.

---

## 7. How the result gets back to the dev

The laptop has local, possibly-uncommitted state; the worker clones from the
remote. The result is delivered as a **GitHub PR** (ADR-006): the worker pushes
a branch and opens a PR, and the IDE surfaces the PR link. This fits the
customer's enforced dev workflow and needs no new delivery mechanism. ADR-006
also weighed two alternatives and set them aside:

- **Branch only.** The worker pushes a branch and the dev pulls it locally.
  Lighter-weight, but skips review gates the customer may rely on.
- **Patch artifact.** The worker produces a diff the IDE applies locally. Most
  "local-feeling", but loses the clean-clone isolation and needs a new
  artifact channel.

A prerequisite either way: the worker operates on **pushed/committed** state, so
the IDE must ensure the base branch (and any WIP it wants acted on) is pushed
before dispatch. This is a workflow contract to confirm with the customer.

---

## 8. Security and multi-tenant concerns

Because the IDE ingress is called by individual developers rather than one
trusted sender, it carries identity and multi-tenant controls in addition to the
shared security controls:

- **Per-developer identity (ADR-001).** A shared webhook secret is not enough:
  the system needs to know *who* dispatched, scope repo access to their
  permissions, and audit it. Identity is established with OIDC via the customer's
  IdP, and feeds `actor` in the payload for audit.
- **Repo authorization.** The static `REPO_ALLOWLIST` (defence in depth) applies
  to both ingresses; per-developer dispatch adds actor-level scoping of which
  repos an identity may target, beyond the global allowlist.
- **GitHub credentials.** The worker uses a server-side `GITHUB_TOKEN` via the gh
  credential helper. Per-developer PR attribution (a bot author noting the dev,
  or per-dev tokens) is a possible future refinement.
- **Concurrency and cost guardrails.** Because parallelism scales in the cloud,
  the ADR-007 guardrails apply: Lambda reserved concurrency, the consumer cap as
  the worker ceiling, an alert-only budget, and a per-actor dispatch quota. See
  ADR-007 and the quota/audit-log section below.
- **Secret redaction, no-token-in-argv, shell=False** apply identically on both
  ingresses.

---

## 9. What is implemented

This ingress is live: the `/tasks` ingress Lambda authenticates (OIDC/JWT), maps
an IDE request to the shared `TaskPayload`, claims the `task_id`, and enqueues on
the shared SQS `JobsQueue`; `GET /tasks/{task_id}` reads the owner-scoped status
row; per-actor quotas, concurrency caps, the alert-only budget, the audit log,
and intermediate status phases are all in place; and the `task_type="spec"`
spec-executor worker runs a whole `tasks.md` (deterministic outer loop, one
commit per task, one PR per spec, stop-at-first-blocker; see
[spec-executor.md](spec-executor.md)).

Intra-spec parallelism for independent tasks is a deferred follow-up: it needs a
`tasks.md` dependency convention (see [spec-executor.md](spec-executor.md) §10).

The whole capability is covered by unit tests that use the in-memory fakes; no
test touches real AWS or GitHub.

---

## 10. Decisions (ADRs)

This capability is built on ADR-001..007, all **ACCEPTED** and implemented; the
full records are in [adr/README.md](adr/README.md):

- **ADR-001**: developer authentication: OIDC via the customer's existing IdP.
- **ADR-002**: task shape: **Option C**, a spec-executor worker variant that
  runs a whole `tasks.md` alongside the `one_shot` scoped path, selected by
  `task_type`. See [spec-executor.md](spec-executor.md).
- **ADR-003**: results channel: polling (with a `phase` field, including the
  spec-executor's `task N/total`).
- **ADR-004**: idempotency key & DynamoDB schema: a generic `task_id` carried
  in the physical `sonar_hash` key; additive schema.
- **ADR-005**: payload naming: generalise to `TaskPayload`, keep field names,
  add `task_type` and the spec-only fields.
- **ADR-006**: result delivery: a GitHub PR (one per spec; draft on a blocker).
- **ADR-007**: concurrency, quota, and cost guardrails for scaled parallelism,
  with per-task/per-job budgets in the spec-executor.
