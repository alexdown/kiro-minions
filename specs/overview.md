# Overview

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out. No human in the loop between the two.

## The core idea: the coding agent is a deterministic pipeline, not a model loop

The coding agent is a **deterministic Python script** running in an **AgentCore Runtime custom container**. It is not a model-in-a-loop. The only AI reasoning happens inside `kiro-cli` — the workflow steps (clone, branch, test, commit, push, PR) are plain sequential code.

This is the **B1 architecture**: one model call (kiro-cli), deterministic everything else. See [runtime.md](runtime.md) for the full design rationale and tradeoffs.

## Flow

The flow is **asynchronous**: the webhook producer Lambda validates, claims, and enqueues, then returns `202` immediately; an SQS-triggered consumer Lambda invokes the long-running Runtime; the Runtime writes its own terminal job status back to DynamoDB.

1. SonarQube automation (or an engineer) creates a Jira ticket for a finding. The ticket carries a `sonarqube` / `SONARQUBE-FIX` label and a `sonar-hash:<key>` label, and its **description contains everything the coding agent needs**.
2. Jira fires a webhook → API Gateway → **orchestrator** (webhook producer) Lambda. The webhook body contains the full ticket: key, summary, description, labels.
3. The producer **authenticates the webhook first** (HMAC / shared-secret, see Security controls), then parses the webhook body directly (no Jira API call needed), extracts the structured fields, builds a `TicketPayload`, **claims** the `sonar_hash` with a DynamoDB lease (idempotency), **enqueues** the payload JSON on the SQS `JobsQueue`, and returns `202`.
4. The SQS `JobsQueue` (with a dead-letter queue, `maxReceiveCount=3`) triggers the **consumer** Lambda, which invokes the AgentCore Runtime with the payload as JSON.
5. The **AgentCore Runtime** container receives the TicketPayload and runs the deterministic pipeline: clone the repo, create the fix branch, run `kiro-cli` headless, run tests (retrying up to 3 times), commit, push, and open a GitHub PR. It then writes its own **terminal job status** (`done` + `pr_url` / `failed` / `tests_failed`) back to DynamoDB.

## The ticket description is the contract

The Jira description is authored as a self-contained task brief in **two parts**:

```
── Part 1: structured header block (machine-readable) ──
Repo URL: https://github.com/your-org/your-repo
Clone:    https://github.com/your-org/your-repo.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

── Part 2: inline SonarQube issue report (human + agent readable) ──
SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 (Critical) · Type: Security Vulnerability · CWE-79 · OWASP A03:2021
File: your-repo/server.js · Line: 135 · Rule: javascript:S5247
Hotspot Key: 6f50f92a-65f2-4c4c-aae5-9b069b44430a

Description / Impact / Recommended Fix:
  swig.setDefaults({ autoescape: true });

References · _sonar-hash: 6f50f92a-... (dedup marker)_
```

The orchestrator reads **Part 1** to populate the TicketPayload fields. The **entire description** (Part 1 + Part 2) is passed to the Runtime in the payload's `description` field — kiro-cli receives it as its brief.

## Architecture

```
            webhook (issue created/updated, label=sonarqube + sonar-hash:<key>)
  Jira  ─────────────────────────────────────────────────►  API Gateway
                                                                  │
                                                                  ▼
                                                          ┌────────────────┐
                                                          │  Orchestrator  │  (Lambda, Python)
                                                          │   (producer)   │
                                                          │ verify webhook │  (HMAC / shared secret)
                                                          │ parse → fields │
                                                          │ claim on hash  │  (DynamoDB, lease)
                                                          │ build payload  │
                                                          │ enqueue → 202  │
                                                          └───────┬────────┘
                                                                  │ sqs:SendMessage
                                                                  │   (TicketPayload JSON)
                                                                  ▼
                                                          ┌────────────────┐
                                                          │   JobsQueue    │  SQS (+ DLQ, maxReceiveCount=3)
                                                          └───────┬────────┘
                                                                  │ event source mapping
                                                                  ▼
                                                          ┌────────────────┐
                                                          │   Consumer     │  (Lambda, Python)
                                                          │ invoke runtime │
                                                          └───────┬────────┘
                                                                  │ invoke(
                                                                  │   runtimeArn,
                                                                  │   runtimeSessionId,
                                                                  │   payload=JSON)
                                                                  ▼
                                            ┌──────────────────────────────────────────┐
                                            │       AgentCore Runtime (custom container) │
                                            │  (isolated microVM · deterministic script) │
                                            │                                            │
                                            │  agent.py — sequential pipeline:           │
                                            │    1. git clone (gh-auth, no token in URL) │
                                            │    2. git checkout -b fix_branch           │
                                            │    3. kiro-cli (headless, brief=desc)      │
                                            │    4. run tests (auto-detect runner)       │
                                            │    5. if fail → reset + retry (≤3x)       │
                                            │    6. git commit + push                    │
                                            │    7. gh pr create                         │
                                            │    8. write job status → DynamoDB          │
                                            │       (done + pr_url / failed / tests_failed) │
                                            └──────────────┬──────────────┬──────────────┘
                                                           │              │
                                                           ▼              ▼
                                                        GitHub PR     DynamoDB (job status)
```

The workflow steps are executed by **deterministic code**, not by a model reasoning in a loop. The only model invocation is kiro-cli (step 3).

## Components

### Orchestrator (`orchestrator/`)
Two Python Lambdas: the **webhook producer** (`handler.handler`) behind API Gateway, and the **SQS consumer** (`handler.sqs_consumer`) triggered by the `JobsQueue`.

**Producer (`handler.handler`):**

- **Authenticates the webhook first:** when `WEBHOOK_SECRET` is configured, the producer verifies an `x-hub-signature-256` HMAC (preferred) or an `x-webhook-secret`/`x-webhook-token` shared-secret header with a constant-time compare (`hmac.compare_digest`) **before** parsing the body or claiming a job. It fails closed (401 missing credential / 403 invalid). When `WEBHOOK_SECRET` is empty it skips verification (fail-open for local/direct invokes). API Gateway default-route throttling caps request rate in front of this.
- **Repo allowlist:** after the header parses, a ticket whose `owner/repo` is not matched by `REPO_ALLOWLIST` (owner/repo globs; empty = allow all) is rejected 403 before any claim/enqueue. The runtime re-checks the same allowlist (defence in depth).
- Validates the incoming webhook body; ignores events without the `sonarqube` / `SONARQUBE-FIX` label.
- **Parses the webhook body directly** — `issue.key`, `issue.fields.summary`, `issue.fields.description`, and `issue.fields.labels` are all present. No Jira API call needed.
- From the description's **Part 1 header block**, extracts: `Clone`, `Base branch`, `File to fix`, `Suggested fix branch` (see [payload.md](payload.md)).
- From the **labels**, extracts the `sonar-hash:<key>` value → `sonar_hash`.
- **Idempotency (lease):** before enqueueing, atomically claims the `sonar_hash` (DynamoDB conditional put) writing `status=in_flight` with `lease_expires_at = now + LEASE_SECONDS`. A claim succeeds only when the row is absent, `failed`, `tests_failed`, or `in_flight` with an **expired lease**; a live `in_flight` or a `done` row blocks (skip).
- **Enqueues** the TicketPayload JSON on the SQS `JobsQueue` and returns `202`. It does **not** invoke the Runtime itself.

**Consumer (`handler.sqs_consumer`):**

- Triggered by the `JobsQueue` event source mapping (the DLQ captures messages that fail `maxReceiveCount=3` times).
- **Invokes the Runtime** via boto3 `bedrock-agentcore` → `invoke`, passing `runtimeArn`, a fresh `runtimeSessionId` (≥ 33 chars), and the TicketPayload as JSON bytes.

### Job statuses, lease, and reclaim

A job row is keyed on `sonar_hash` and moves through these statuses:

- `in_flight` — claimed by the producer with a numeric `lease_expires_at` lease.
- `done` — terminal success (the runtime wrote it, with `pr_url`); blocks re-dispatch.
- `failed` — terminal failure; **reclaimable** (a later webhook can re-claim).
- `tests_failed` — the runtime opened a draft PR / produced no usable diff; **reclaimable**.

`_claim_job` only re-claims when the row is absent, `failed`, `tests_failed`, or `in_flight` **with an expired lease**, so a crashed or timed-out consumer cannot wedge a hash forever. DynamoDB TTL is enabled on `lease_expires_at` for best-effort cleanup only; the lease comparison in `_claim_job` is the authoritative reclaim gate.

### Security controls

- **Webhook authentication** — HMAC (`x-hub-signature-256`) or shared-secret header, constant-time compare. **Fail-closed** when `WEBHOOK_SECRET` is set (401/403), **fail-open** when empty.
- **Repo allowlist** — owner/repo globs, enforced in the producer and **re-checked in the runtime** (defence in depth).
- **Secret redaction** — `redact()` masks `GITHUB_TOKEN` / `KIRO_API_KEY` / `WEBHOOK_SECRET` so secrets never reach logs, exceptions, or responses.
- **No token in argv/URL** — git/gh auth uses the **gh credential helper** (`gh auth setup-git` reads `GITHUB_TOKEN` from the environment); the token is never placed in a clone URL or command argument.
- **`shell=False` for ticket-derived data** — subprocess calls built from ticket fields pass argument lists, never a shell string.
- **Input validation** — `validate_clone_url` (https only) and `validate_branch_name` (reject shell metacharacters) run before any field reaches git/gh.

### Runtime (`runtime/`)
A custom Docker container (ARM64) deployed as an AgentCore Runtime:

- `runtime/Dockerfile` — pre-installs kiro-cli, gh, git, AWS CLI, Node.js.
- `runtime/agent.py` — the deterministic pipeline script (BedrockAgentCoreApp entrypoint).
- `runtime/requirements.txt` — Python dependencies (bedrock-agentcore, boto3).
- `runtime/.bedrock_agentcore.yaml` — deployment configuration for `agentcore launch`.

The pipeline receives the TicketPayload and runs:
1. Configure git auth via the gh credential helper (`gh auth setup-git`), then clone the repo — the `GITHUB_TOKEN` is read from the environment and is **never** placed in the clone URL or argv
2. Create fix branch
3. Run kiro-cli headless with the issue description as the brief
4. Auto-detect and run the test suite
5. If tests fail, reset and retry (up to `MAX_FIX_ATTEMPTS`, default 3)
6. Commit with message `fix: {summary} [{ticket_id}]`
7. Push the fix branch
8. Open a PR via `gh pr create`
9. Write the terminal job status (`done` + `pr_url` / `failed` / `tests_failed`) to DynamoDB

### Infrastructure (`deploy/`)
AWS SAM template providing:
- API Gateway HTTP API (webhook endpoint) with stage-level `DefaultRouteSettings` throttling
- Lambda functions (webhook producer + SQS consumer)
- SQS `JobsQueue` + dead-letter queue (`RedrivePolicy`, `maxReceiveCount=3`)
- DynamoDB table (idempotency/lease on sonar_hash, TTL on `lease_expires_at`)
- IAM policies (InvokeRuntime on AgentCore, DynamoDB CRUD, SQS send/receive)

## Credentials (env vars)

| Var | Lives in | Used by | Purpose |
|-----|----------|---------|---------|
| `GITHUB_TOKEN` | Runtime | git, gh | Clone, push, open PR |
| `KIRO_API_KEY` | Runtime | kiro-cli | Headless auth |
| `AGENTCORE_RUNTIME_ARN` | Orchestrator | handler.py | Which Runtime to invoke |

The orchestrator needs **no external credentials** beyond AWS IAM.

## Design decision: B1 (deterministic) over managed harness

See [runtime.md](runtime.md) for the full comparison. Summary:

- **Previous (managed harness):** two nested model loops — the harness model reasons about git/test/PR workflow, then calls kiro-cli which does its own reasoning. Double token cost, double latency, model can deviate from the script.
- **Current (B1 Runtime):** one model call (kiro-cli). Everything else is sequential Python. Predictable, testable, cheaper, faster.
- **Future (B2):** if needed, add model reasoning only for error recovery on novel failures. Keep the happy path deterministic.
