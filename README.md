# kiro-minions

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out.

```
Jira ticket (label=sonarqube + sonar-hash:<key>)
  → webhook → API Gateway → Orchestrator (Lambda, Python)
      → parse webhook body directly (no Jira API call)
      → extract structured header (clone URL, base branch, fix branch, file) + sonar_hash label
      → dedup / claim on sonar_hash; enqueue TicketPayload JSON on SQS → return 202
  → SQS work queue (+ DLQ) → Consumer (Lambda, Python)
      → invoke AgentCore Runtime with the TicketPayload JSON
  → Runtime (custom container — deterministic pipeline, no outer model loop)
      → git clone → checkout base_branch → create fix_branch
      → kiro-cli headless (brief = ticket description) → run tests
      → retry loop (up to 3 attempts) → commit → push → open PR
      → write terminal job status (done/failed/tests_failed) to DynamoDB
  → GitHub PR (against base_branch)
```

## Architecture: B1 (deterministic pipeline)

The coding agent is a **deterministic Python script** running in an **AgentCore Runtime** custom container. There is no outer model loop — the only AI reasoning happens inside `kiro-cli` itself. The workflow steps (clone, branch, test, commit, push, PR) are plain sequential code.

**Why B1 over a managed harness?**

- **Single agent loop** — kiro-cli is the only model invocation. No tokens wasted on deciding to run `git clone`.
- **Predictable** — the workflow executes the same way every time. No model deviation.
- **Testable** — the pipeline is Python code you can unit-test and mock.
- **Fast** — deterministic steps run at shell speed; only the fix step waits for model inference.

## Flow

The pipeline is **asynchronous**: the webhook Lambda validates, claims the hash,
and enqueues the payload, then returns `202` immediately. A separate SQS-triggered
consumer Lambda invokes the long-running AgentCore Runtime, and the Runtime writes
its own terminal job status back to DynamoDB.

```
            webhook (issue created/updated, label=sonarqube + sonar-hash:<key>)
  Jira  ─────────────────────────────────────────────────►  API Gateway
                                                                  │
                                                                  ▼
                                                          ┌────────────────┐
                                                          │  Orchestrator  │  (Lambda, Python)
                                                          │                │
                                                          │ parse webhook  │
                                                          │ body → fields  │
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
                                                                  │   payload=TicketPayload JSON)
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

## Layout

```
orchestrator/   Lambdas: webhook producer (handler.handler) + SQS consumer (handler.sqs_consumer)
runtime/        AgentCore Runtime container (Dockerfile + deterministic pipeline script)
agent/          [LEGACY] Original managed-harness definition (kept for reference)
specs/          Design docs
deploy/         SAM template (API Gateway + Lambdas + SQS + DLQ + DynamoDB)
tests/          Unit tests
```

## Testing

Dev/test dependencies are pinned in `requirements-dev.txt` (they are **not**
shipped to the Lambda orchestrator or the Runtime container). Create a
virtualenv at the repo root, install them, and run the suite:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && python -m pytest tests/ -q
```

Once the venv is set up, the recurring one-liner is just:

```bash
python -m pytest tests/ -q
```

Tests must never touch real AWS, GitHub, Jira or kiro-cli — use the in-memory
fakes and SDK stubs under `tests/` (`tests/fakes.py` + `tests/conftest.py`).
The stubs inject fake `boto3` / `botocore` / `bedrock_agentcore` modules into
`sys.modules` so the suite runs with zero installs, which is why
`requirements-dev.txt` is only needed if you want the real SDKs available.

## Example ticket (PROJ-15)

```
Title: [SonarQube][P0] XSS — Swig template auto-escaping disabled (server.js:135)

Repo URL: https://github.com/your-org/your-repo
Clone: https://github.com/your-org/your-repo.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 · CWE-79 · OWASP A03:2021 · Rule javascript:S5247
Recommended Fix:  swig.setDefaults({ autoescape: true });
_sonar-hash: 6f50f92a-65f2-4c4c-aae5-9b069b44430a_

Labels: CWE-79, OWASP-A03, P0, SONARQUBE-FIX, XSS, security,
        sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a, sonarqube
```

## Deploy

### 1. Deploy the Runtime container

```bash
cd runtime

# Set your AWS account ID
export AWS_ACCOUNT_ID=123456789012

# Build and deploy via AgentCore starter toolkit
agentcore launch
```

This builds the ARM64 Docker image (with kiro-cli, gh, git pre-installed), pushes to ECR, and deploys an AgentCore Runtime. Note the **Runtime ARN** from the output.

### 2. Deploy the orchestrator stack

```bash
cd deploy

sam deploy \
  --stack-name kiro-minions \
  --parameter-overrides \
    AgentCoreRuntimeArn=arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/kiro-minions \
    JiraBaseUrl=https://your-org.atlassian.net \
    WebhookSecretSsmName=/kiro-minions/webhook-secret \
    RepoAllowlist='your-org/*' \
  --capabilities CAPABILITY_IAM
```

The webhook secret itself is **never** passed on the command line: store it once
as a SecureString SSM parameter and pass only its *name* via
`WebhookSecretSsmName`. The template resolves the value at deploy time with
`{{resolve:ssm-secure:...}}` (see `deploy/template.yaml`).

### 3. Configure the Runtime environment and IAM

The Runtime container needs these env vars (set via AgentCore Runtime environment
configuration or the execution role's access to SSM):

| Var | Purpose |
|-----|---------|
| `GITHUB_TOKEN` | Clone, push, open PR (read from the environment by a git credential helper — never placed in the clone URL) |
| `KIRO_API_KEY` | kiro-cli headless auth |
| `DYNAMODB_TABLE` | Jobs table the runtime writes its terminal status to (share the SAM `JobsTableName` output — the orchestrator and runtime use the **same** table) |
| `REPO_ALLOWLIST` | Owner/repo glob patterns the runtime re-checks before cloning (defence in depth; set to the **same** value as the orchestrator's `RepoAllowlist`; empty = allow all) |
| `AWS_REGION` | Region for the runtime's DynamoDB client (usually injected automatically) |

**Runtime execution role — DynamoDB permissions (attach out-of-band).** Because
the runtime now writes its own terminal job status (`done` + `pr_url`, `failed`,
or `tests_failed`) to the jobs table, its **execution role** needs DynamoDB write
access. The AgentCore Runtime role is created by `agentcore launch`, **not** by
this SAM stack, so the template cannot attach it — the operator must add the
following to the Runtime execution role manually:

| Action | Why |
|--------|-----|
| `dynamodb:UpdateItem` | `write_job_status` sets `status` + `pr_url`/`reason` |
| `dynamodb:PutItem` | defensive (status writes use `UpdateItem` today) |
| `dynamodb:GetItem` | read-back / future lease inspection |

Resource: the jobs table ARN (the `JobsTableArn` output of the SAM stack).

### 4. Wire the Jira webhook

Point your Jira webhook at the `WebhookUrl` output from the SAM stack.

## Credentials

| Var | Lives in | Used by |
|-----|----------|---------|
| `GITHUB_TOKEN` | Runtime container env | git clone/push, gh pr create (via credential helper, never in argv/URL) |
| `KIRO_API_KEY` | Runtime container env | kiro-cli headless |
| `DYNAMODB_TABLE` | Runtime + both Lambdas env | shared jobs table (claim + terminal status) |
| `AGENTCORE_RUNTIME_ARN` | Both Lambdas env | which Runtime to invoke |
| `SQS_QUEUE_URL` | Orchestrator (producer) Lambda env | work queue to enqueue TicketPayloads onto |
| `LEASE_SECONDS` | Orchestrator (producer) Lambda env | claim lease validity (default `3600`); after it expires a stale `in_flight` row is reclaimable |
| `WEBHOOK_SECRET` | Orchestrator (producer) Lambda env | webhook shared secret / HMAC key (resolved from SSM at deploy; **never** hardcoded). When set, `verify_webhook` **fails closed** (401 for a missing credential header, 403 for an invalid signature/secret); when empty, verification is skipped (fail-open) for local/direct invokes and unconfigured deployments. All compares use `hmac.compare_digest` (constant-time) |
| `REPO_ALLOWLIST` | Orchestrator (producer) Lambda env | comma/whitespace separated owner/repo glob patterns; a ticket whose repo is not allowed is rejected 403 before any claim/enqueue. Empty = allow all. Re-checked in the runtime |

### Webhook authentication (`WEBHOOK_SECRET`)

Incoming webhooks are authenticated **before** any body parsing or job claim.
Two modes are supported, in precedence order:

1. **HMAC signature** — when an `x-hub-signature-256` header is present, the
   orchestrator computes `hmac-sha256(secret, raw_body)` and compares it
   (constant-time) against the header (hex, optional `sha256=` prefix).
2. **Shared secret** — otherwise, an `x-webhook-secret` (or `x-webhook-token`)
   header is compared (constant-time) against `WEBHOOK_SECRET`.

The secret value never appears in a response body, log line, or exception (it is
part of `redact()` coverage). API Gateway stage-level throttling defaults
(`DefaultRouteSettings`) on the HTTP API add a rate/burst cap in front of this.

The orchestrator and consumer need **no external credentials** beyond AWS IAM: the
producer calls DynamoDB + SQS `SendMessage`; the consumer calls SQS (receive/delete)
+ AgentCore `InvokeRuntime`; the runtime writes DynamoDB job status (see IAM above).

### Job lifecycle, lease, and TTL

A job row is keyed on `sonar_hash`. The producer's `_claim_job` writes `status=in_flight`
with a numeric `lease_expires_at = now + LEASE_SECONDS`. A re-dispatch is only claimed
when the row is absent, `failed`, or `in_flight` **with an expired lease** — so a
crashed or timed-out consumer cannot wedge a `sonar_hash` forever. The runtime later
overwrites the row with its terminal status (`done` + `pr_url`, `failed`, or
`tests_failed`). The DynamoDB table has TTL enabled on `lease_expires_at` for
best-effort cleanup only; the lease comparison in `_claim_job` is the authoritative
reclaim gate, independent of when the TTL sweeper runs.

## Docs

- [specs/overview.md](specs/overview.md) — architecture and design rationale
- [specs/payload.md](specs/payload.md) — payload schema + ticket parsing
- [specs/runtime.md](specs/runtime.md) — Runtime container design and deployment
- [specs/ide-ingress.md](specs/ide-ingress.md) — **DRAFT** proposal: Kiro IDE task ingress (remote worker hand-off)
- [specs/adr/README.md](specs/adr/README.md) — Architecture Decision Registry (decisions pending review)
