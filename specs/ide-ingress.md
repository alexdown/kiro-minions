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
- Running arbitrary "implement this whole feature" tasks **if** ADR-002 lands on
  the scoped-task option (see §4 and ADR-002).

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
| AgentCore Runtime pipeline (`agent.py`) | Partly (assumes a scoped fix) | Reuse; see ADR-002 for task shape |
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
SonarQube finding in this file." The deterministic pipeline is built around that
shape — one `kiro-cli` brief, auto-detected tests, up to 3 retries, one PR.

"Work assigned from an IDE" is open-ended. The single most important decision
(ADR-002) is which of these we support in v1:

- **Option A — Scoped tasks (recommended v1).** The IDE sends a bounded task:
  repo + base branch + a target (file/area) + a natural-language instruction +
  acceptance = tests pass. This maps almost 1:1 onto today's pipeline and the
  `description` → kiro-cli brief flow. Lowest risk, fastest to ship, keeps B1.
- **Option B — Open-ended tasks.** "Implement feature X across the repo." This
  likely needs the plan→code→review loop (a richer worker), not the single-shot
  deterministic pipeline. Bigger build, new failure modes, higher cost per task,
  and it reopens the "outer model loop" question the B1 design deliberately
  avoided.

**Recommendation:** ship **Option A** first (reuses the runtime, proves the
hand-off and scaling), and treat Option B as a follow-up that may introduce a
*second* runtime variant rather than changing the deterministic one. This needs
your decision — see ADR-002.

---

## 5. Payload contract (generalising `TicketPayload` → `TaskPayload`)

Today the runtime consumes a `TicketPayload` (see [payload.md](payload.md)). The
IDE ingress should produce the **same downstream contract** so SQS/consumer/
runtime don't change. Proposed generalisation (names TBD — ADR-005):

```jsonc
{
  "task_id":        "ide-7f3c…",          // idempotency key (ADR-004)
  "source":         "kiro-ide",           // vs "jira"; for routing/audit
  "actor":          "dev@customer.com",   // who dispatched (from auth, ADR-001)
  "repo_clone_url": "https://github.com/org/repo.git",
  "base_branch":    "main",
  "fix_branch":     "kiro/ide-7f3c…",     // or caller-supplied (validated)
  "file_to_fix":    "src/foo.ts:42",      // optional scope hint
  "description":    "…natural-language instruction = kiro-cli brief…",
  "summary":        "short title for the PR/commit",
  "acceptance":     "tests_pass"          // v1 fixed; future: lint, typecheck…
}
```

- Fields that already exist keep their names and validation
  (`validate_clone_url`, `validate_branch_name`) unchanged.
- `sonar_hash` (Jira-only) becomes an optional `source`-specific field; the
  generic idempotency key is `task_id` (ADR-004). The DynamoDB key rename has
  migration implications — see ADR-004.
- The runtime needs **no change** if we keep `description` as the kiro-cli brief
  and `acceptance=tests_pass`.

---

## 6. Results / feedback channel

Jira ingress is fire-and-forget (the PR shows up later). A developer in an IDE
expects feedback. Options (ADR-003):

- **Option A — Polling (recommended v1).** `GET /tasks/{task_id}` returns the
  current status row from DynamoDB (`queued` / `in_flight` / `done`+`pr_url` /
  `failed` / `tests_failed` + a redacted reason). The extension polls every few
  seconds. Simplest, stateless, cheap, reuses the status row we already write.
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

1. **Spike / design (this doc + ADRs).** Lock ADR-001/002/003/004/006/007.
2. **Thin PoC:** new ingress Lambda + `/tasks` route that authenticates, maps an
   IDE request → `TaskPayload`, claims, and enqueues onto the **existing** SQS.
   Reuse everything downstream. Add `GET /tasks/{id}` reading the status row.
   Prove one task dispatched from a script end-to-end to a PR.
3. **IDE integration:** customer extension calls `/tasks` + polls `/tasks/{id}`.
4. **Hardening:** per-actor quotas, concurrency caps, cost alarms, audit log,
   intermediate status phases for progress UX.
5. **(Optional) Option B** open-ended tasks as a separate worker variant.

Each phase ships with unit tests using the existing fakes; no real AWS/GitHub in
tests; keep the diff focused per phase.

---

## 10. Open questions for you

These are captured as ADRs needing a decision in [adr/README.md](adr/README.md):

- **ADR-001** — developer authentication mechanism.
- **ADR-002** — task shape: scoped (A) vs open-ended (B) for v1.
- **ADR-003** — results channel: polling (A) vs streaming (B).
- **ADR-004** — idempotency key & DynamoDB schema (reuse `sonar_hash` table vs
  new generic `task_id` / new table).
- **ADR-005** — payload naming (`TicketPayload` + `source` vs new `TaskPayload`).
- **ADR-006** — result delivery: PR / branch / patch.
- **ADR-007** — concurrency, quota, and cost guardrails for scaled parallelism.

Nothing here is built yet — this branch is specs only. Once you've recorded the
decisions in the ADRs, a fresh session can pick up Phase 2 (the thin PoC).
